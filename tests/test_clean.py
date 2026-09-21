"""The cleanup runner and its safety guarantees.

The properties tested here are the ones the whole `clean` design rests on: a
dry run plans exactly what an apply would send, planning never mutates, a
failed step abandons the rest of its finding, and a check with no safe plan
refuses with a reason instead of guessing.
"""

from __future__ import annotations

import httplib2
import pytest
from googleapiclient.errors import HttpError

from tests.conftest import FakeClient
from zombiescan import clean
from zombiescan.cleaners import CLEANERS, Step, backup_name
from zombiescan.models import Finding
from zombiescan.registry import CHECKS


class RecordingClients:
    """Clients that record every call, so a test can assert nothing was sent."""

    def __init__(self, responses=None, fails_on=None):
        self.client = FakeClient(responses or {})
        self.fails_on = fails_on

    def get(self, api):
        if self.fails_on:
            return _FailingClient(self.client, self.fails_on)
        return self.client

    @property
    def calls(self):
        return self.client.call_log


class _FailingClient:
    def __init__(self, inner, fails_on):
        self._inner = inner
        self._fails_on = fails_on

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _finding(check="unattached-disk", location="us-central1-a", cost=10.0, resource="disk-1"):
    return Finding(
        check=check,
        resource_id=resource,
        resource_type="compute-disk",
        project="proj-1",
        location=location,
        reason="unattached",
        monthly_cost=cost,
        remediation="gcloud compute disks delete disk-1",
    )


@pytest.fixture
def clients():
    return RecordingClients()


# --------------------------------------------------------------------------
# planning


def test_planning_sends_nothing(clients, pricing):
    outcome = clean.plan_for(clients, _finding(), pricing)
    assert outcome.status == clean.PLANNED
    assert clients.calls == []


def test_a_disk_is_snapshotted_before_it_is_deleted(clients, pricing):
    """A failed step abandons the rest, so the backup has to come first."""
    outcome = clean.plan_for(clients, _finding(), pricing)
    operations = [step.operation for step in outcome.steps]
    assert operations == ["disks.createSnapshot", "disks.delete"]


def test_a_regional_disk_uses_the_regional_collection(clients, pricing):
    outcome = clean.plan_for(clients, _finding(location="us-central1"), pricing)
    assert [s.operation for s in outcome.steps] == [
        "regionDisks.createSnapshot",
        "regionDisks.delete",
    ]
    assert outcome.steps[0].params["region"] == "us-central1"


def test_a_global_address_and_a_regional_one_take_different_calls(clients, pricing):
    regional = clean.plan_for(
        clients,
        _finding(check="unused-static-ip", location="us-central1", resource="ip-1"),
        pricing,
    )
    globally = clean.plan_for(
        clients, _finding(check="unused-static-ip", location="global", resource="ip-2"), pricing
    )
    assert regional.steps[0].operation == "addresses.delete"
    assert globally.steps[0].operation == "globalAddresses.delete"


def test_releasing_an_address_is_marked_irreversible(clients, pricing):
    outcome = clean.plan_for(clients, _finding(check="unused-static-ip", resource="ip-1"), pricing)
    assert outcome.irreversible is True


def test_destroying_a_kms_version_is_not_irreversible(clients, pricing):
    """Google holds a destroyed key version for 24 hours; that is a real recovery path."""
    finding = _finding(check="disabled-kms-key", location="us-central1", resource="ring/key/3")
    outcome = clean.plan_for(clients, finding, pricing)
    assert outcome.status == clean.PLANNED
    assert outcome.irreversible is False
    assert outcome.steps[0].params["name"].endswith("/cryptoKeyVersions/3")


def test_a_sql_instance_is_backed_up_before_deletion(clients, pricing):
    outcome = clean.plan_for(
        clients, _finding(check="stopped-sql-instance", resource="db-1"), pricing
    )
    assert [s.operation for s in outcome.steps] == ["backupRuns.insert", "instances.delete"]


def test_a_check_that_refuses_says_why(clients, pricing):
    outcome = clean.plan_for(
        clients, _finding(check="empty-vpc-network", resource="default"), pricing
    )
    assert outcome.status == clean.UNSUPPORTED
    assert "cannot be deleted until everything inside it is gone" in outcome.error
    assert outcome.steps == []


def test_a_check_this_build_does_not_have_names_the_missing_pack(clients, pricing):
    outcome = clean.plan_for(clients, _finding(check="some-third-party-check"), pricing)
    assert outcome.status == clean.UNSUPPORTED
    assert "install the pack that produced this report" in outcome.error


def test_a_planner_that_raises_fails_that_finding_only(clients, pricing):
    """An instance whose get call fails must not abort the rest of the run."""
    outcome = clean.plan_for(clients, _finding(check="stopped-instance", resource="vm-1"), pricing)
    assert outcome.status == clean.FAILED
    assert "could not plan" in outcome.error


def test_every_check_has_a_cleaner_or_an_explicit_refusal():
    """Refuse rather than guess only works if the refusal explains itself."""
    silent = [
        name for name, spec in CHECKS.items() if name not in CLEANERS and not spec.uncleanable
    ]
    assert silent == [], f"checks with neither a cleaner nor a reason: {silent}"


def test_a_check_never_has_both_a_cleaner_and_a_refusal():
    """A refusal that a cleaner contradicts leaves the operator guessing which wins."""
    both = [name for name, spec in CHECKS.items() if name in CLEANERS and spec.uncleanable]
    assert both == []


# --------------------------------------------------------------------------
# applying


def test_apply_sends_each_step_in_order():
    clients = RecordingClients(
        {"disks.createSnapshot": {"name": "op-1"}, "disks.delete": {"name": "op-2"}}
    )
    outcome = clean.Outcome(
        finding=_finding(),
        steps=[
            Step("snapshot", "compute", "disks.createSnapshot", {"project": "p"}),
            Step("delete", "compute", "disks.delete", {"project": "p"}),
        ],
    )
    clean.apply_outcome(outcome, clients)
    assert outcome.status == clean.APPLIED
    assert [op for op, _ in clients.calls] == ["disks.createSnapshot", "disks.delete"]
    assert outcome.monthly_saving == 10.0


def test_a_failed_step_abandons_the_destructive_one_behind_it():
    """The whole reason backups are planned first."""

    class Failing:
        def get(self, api):
            raise HttpError(httplib2.Response({"status": 500}), b"quota exceeded", uri="u")

    outcome = clean.Outcome(
        finding=_finding(),
        steps=[
            Step("snapshot", "compute", "disks.createSnapshot", {}),
            Step("delete", "compute", "disks.delete", {}, irreversible=True),
        ],
    )
    clean.apply_outcome(outcome, Failing())
    assert outcome.status == clean.FAILED
    assert outcome.results == []
    assert outcome.monthly_saving == 0.0


def test_only_applied_outcomes_count_as_savings():
    outcome = clean.Outcome(finding=_finding(), status=clean.SKIPPED)
    assert outcome.monthly_saving == 0.0


# --------------------------------------------------------------------------
# the audit record


def test_the_audit_says_which_mode_it_ran_in():
    outcomes = [clean.Outcome(finding=_finding(), status=clean.APPLIED)]
    assert clean.audit_document(outcomes, applied=False)["mode"] == "dry-run"
    assert clean.audit_document(outcomes, applied=True)["mode"] == "apply"


def test_the_audit_records_the_project_and_location_of_each_action():
    outcomes = [clean.Outcome(finding=_finding(), status=clean.APPLIED)]
    action = clean.audit_document(outcomes, applied=True, principal="me@example.com")["actions"][0]
    assert action["project"] == "proj-1"
    assert action["location"] == "us-central1-a"


def test_backup_names_are_unique_and_legal():
    """Compute rejects a name over 63 characters or ending in a hyphen."""
    name = backup_name("x" * 80)
    assert len(name) <= 63
    assert not name.endswith("-")
    assert backup_name("disk-1").startswith("disk-1-zombiescan-")
