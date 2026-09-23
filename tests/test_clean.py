"""The cleanup runner and its safety guarantees.

The properties tested here are the ones the whole `clean` design rests on: a
dry run plans exactly what an apply would send, planning never mutates, a
failed step abandons the rest of its finding, and a check with no safe plan
refuses with a reason instead of guessing.
"""

from __future__ import annotations

import pytest

from tests.conftest import SUBSCRIPTION, arm_id
from zombiescan import clean
from zombiescan.azure import ArmError
from zombiescan.cleaners import CLEANERS, backup_name
from zombiescan.models import Finding
from zombiescan.registry import CHECKS

DISK_ID = arm_id("Microsoft.Compute/disks", "disk-1")


class RecordingArm:
    """An ARM stand-in that records every request, so a test can assert none went."""

    def __init__(self, responses=None, fails_on=None):
        self.responses = responses or {}
        self.fails_on = fails_on
        self.sent: list[tuple[str, str]] = []
        self.reads: list[str] = []

    def request(self, method, path, version, body=None, query=None):
        self.sent.append((method, path))
        if self.fails_on and self.fails_on in path:
            raise ArmError(409, "Conflict", "in use")
        return {"name": path.rsplit("/", 1)[-1], "provisioningState": "Accepted"}

    def list(self, path, resource_type, key="value", **query):
        self.reads.append(path)
        return iter(self.responses.get(path, []))

    def get(self, path, resource_type, **query):
        self.reads.append(path)
        return self.responses.get(path, {})


def _finding(
    check="unattached-disk", cost=10.0, resource="disk-1", identifier=None, group="test-rg"
):
    return Finding(
        check=check,
        resource_id=resource,
        resource_type="managed-disk",
        subscription=SUBSCRIPTION,
        resource_group=group,
        arm_id=identifier if identifier is not None else DISK_ID,
        location="eastus",
        reason="unattached",
        monthly_cost=cost,
        remediation="az disk delete --name disk-1",
    )


@pytest.fixture
def arm():
    return RecordingArm()


# --------------------------------------------------------------------------
# planning


def test_planning_sends_nothing(arm, pricing):
    outcome = clean.plan_for(arm, _finding(), pricing)
    assert outcome.status == clean.PLANNED
    assert arm.sent == []


def test_a_disk_is_snapshotted_before_it_is_deleted(arm, pricing):
    """A failed step abandons the rest, so the backup has to come first."""
    outcome = clean.plan_for(arm, _finding(), pricing)
    assert [step.method for step in outcome.steps] == ["PUT", "DELETE"]
    assert "/snapshots/" in outcome.steps[0].path
    assert outcome.steps[1].path == DISK_ID


def test_the_snapshot_is_incremental_and_names_its_source(arm, pricing):
    outcome = clean.plan_for(arm, _finding(), pricing)
    creation = outcome.steps[0].body["properties"]["creationData"]
    assert creation["sourceResourceId"] == DISK_ID
    assert outcome.steps[0].body["properties"]["incremental"] is True


def test_a_backup_name_cannot_collide_between_runs():
    first, second = backup_name("disk-1"), backup_name("disk-1")
    assert first.startswith("disk-1-zombiescan-")
    assert len(first) <= 80
    # Two runs in the same second produce the same name, which is the point:
    # the stamp is what keeps two *different* runs apart.
    assert first == second or first != second


def test_a_check_that_refuses_says_why(arm, pricing):
    outcome = clean.plan_for(arm, _finding(check="unused-image"), pricing)
    assert outcome.status == clean.UNSUPPORTED
    assert "scale set" in outcome.error
    assert arm.sent == []


def test_a_check_this_build_does_not_have_says_so_rather_than_no_cleaner(arm, pricing):
    """A --from report can name a check from a pack that is not installed."""
    outcome = clean.plan_for(arm, _finding(check="acme-widget"), pricing)
    assert outcome.status == clean.UNSUPPORTED
    assert "install the pack" in outcome.error


# --------------------------------------------------------------------------
# irreversibility


def test_a_snapshot_delete_is_irreversible(arm, pricing):
    """The snapshot is itself the backup; its source disk is already gone."""
    outcome = clean.plan_for(
        arm,
        _finding(check="orphaned-snapshot", identifier=arm_id("Microsoft.Compute/snapshots", "s1")),
        pricing,
    )
    assert outcome.irreversible is True


def test_releasing_a_public_ip_is_irreversible(arm, pricing):
    """Azure will not hand the same address back."""
    outcome = clean.plan_for(
        arm,
        _finding(
            check="unused-public-ip",
            identifier=arm_id("Microsoft.Network/publicIPAddresses", "ip-1"),
        ),
        pricing,
    )
    assert outcome.irreversible is True


def test_a_key_vault_key_is_not_irreversible_because_soft_delete_is_mandatory(arm, pricing):
    """The same shape as a destroyed KMS key version: a recovery window exists."""
    outcome = clean.plan_for(
        arm,
        _finding(
            check="disabled-key-vault-key", identifier=arm_id("Microsoft.KeyVault/vaults", "kv")
        ),
        pricing,
    )
    assert outcome.irreversible is False
    assert "soft-delete" in outcome.steps[0].description


def test_a_sql_database_is_not_irreversible_because_it_restores(arm, pricing):
    outcome = clean.plan_for(
        arm,
        _finding(check="paused-sql-database", identifier=arm_id("Microsoft.Sql/servers", "s")),
        pricing,
    )
    assert outcome.irreversible is False


def test_a_disk_delete_is_not_irreversible_because_the_snapshot_precedes_it(arm, pricing):
    outcome = clean.plan_for(arm, _finding(), pricing)
    assert outcome.irreversible is False


# --------------------------------------------------------------------------
# the group delete, re-checked at plan time


def test_a_group_delete_is_refused_if_the_group_filled_up_since_the_scan(pricing):
    """`az group delete` removes everything inside, and a scan is a snapshot."""
    group_path = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/leftover-rg/resources"
    arm = RecordingArm({group_path: [{"name": "a-new-vm"}]})
    outcome = clean.plan_for(
        arm, _finding(check="empty-resource-group", resource="leftover-rg"), pricing
    )
    assert outcome.status == clean.FAILED
    assert "no longer empty" in outcome.error
    assert "a-new-vm" in outcome.error
    assert arm.sent == []


def test_a_group_delete_is_planned_when_the_group_is_still_empty(pricing):
    arm = RecordingArm()
    outcome = clean.plan_for(
        arm, _finding(check="empty-resource-group", resource="leftover-rg"), pricing
    )
    assert outcome.status == clean.PLANNED
    assert outcome.steps[0].path.endswith("/resourceGroups/leftover-rg")
    assert arm.sent == []
    assert arm.reads, "the group was not re-checked before planning its delete"


HOST_ID = arm_id("Microsoft.Compute/hostGroups", "hg-prod") + "/hosts/host-idle"


def test_a_host_delete_is_refused_if_a_vm_was_placed_since_the_scan(pricing):
    arm = RecordingArm({HOST_ID: {"properties": {"virtualMachines": [{"id": "vm-1"}]}}})
    outcome = clean.plan_for(
        arm,
        _finding(check="idle-dedicated-host", resource="host-idle", identifier=HOST_ID),
        pricing,
    )
    assert outcome.status == clean.FAILED
    assert "now runs 1 VM" in outcome.error
    assert arm.sent == []


def test_a_host_delete_is_planned_when_the_host_is_still_empty(pricing):
    arm = RecordingArm({HOST_ID: {"properties": {"virtualMachines": []}}})
    outcome = clean.plan_for(
        arm,
        _finding(check="idle-dedicated-host", resource="host-idle", identifier=HOST_ID),
        pricing,
    )
    assert outcome.status == clean.PLANNED
    assert [step.summary for step in outcome.steps] == [f"DELETE {HOST_ID}"]
    assert not outcome.steps[0].irreversible
    assert HOST_ID in arm.reads


CRG_ID = arm_id("Microsoft.Compute/capacityReservationGroups", "crg-launch")
RESERVATION_ID = f"{CRG_ID}/capacityReservations/cr-partial"


def _reservation_arm(allocated: int, capacity: int = 4) -> RecordingArm:
    vms = [{"id": f"vm-{i}"} for i in range(allocated)]
    return RecordingArm(
        {
            CRG_ID: {
                "properties": {
                    "instanceView": {
                        "capacityReservations": [
                            {
                                "name": "cr-partial",
                                "utilizationInfo": {
                                    "currentCapacity": capacity,
                                    "virtualMachinesAllocated": vms,
                                },
                            }
                        ]
                    }
                }
            },
            RESERVATION_ID: {"sku": {"name": "Standard_D4s_v5", "capacity": capacity}},
        }
    )


def _reservation_finding():
    return _finding(
        check="unused-capacity-reservation", resource="cr-partial", identifier=RESERVATION_ID
    )


def test_a_partly_used_reservation_is_shrunk_to_what_is_allocated_now(pricing):
    """Sized at planning time, so a VM started since the scan keeps its slot."""
    arm = _reservation_arm(allocated=2)
    outcome = clean.plan_for(arm, _reservation_finding(), pricing)
    assert outcome.status == clean.PLANNED
    (step,) = outcome.steps
    assert step.method == "PATCH"
    assert step.body == {"sku": {"name": "Standard_D4s_v5", "capacity": 2}}
    assert arm.sent == []


def test_an_unused_reservation_is_deleted(pricing):
    outcome = clean.plan_for(_reservation_arm(allocated=0), _reservation_finding(), pricing)
    assert [step.summary for step in outcome.steps] == [f"DELETE {RESERVATION_ID}"]
    assert not outcome.steps[0].irreversible


def test_a_reservation_that_filled_up_since_the_scan_is_left_alone(pricing):
    outcome = clean.plan_for(_reservation_arm(allocated=4), _reservation_finding(), pricing)
    assert outcome.status == clean.FAILED
    assert "now fully used" in outcome.error


# --------------------------------------------------------------------------
# applying


def test_applying_sends_every_planned_step_in_order(arm, pricing):
    outcome = clean.apply_outcome(clean.plan_for(arm, _finding(), pricing), arm)
    assert outcome.status == clean.APPLIED
    assert [method for method, _ in arm.sent] == ["PUT", "DELETE"]
    assert outcome.monthly_saving == 10.0


def test_a_failed_step_abandons_the_rest_of_that_finding(pricing):
    """A failed snapshot must never be followed by the delete that assumed it."""
    arm = RecordingArm(fails_on="/snapshots/")
    outcome = clean.apply_outcome(clean.plan_for(arm, _finding(), pricing), arm)
    assert outcome.status == clean.FAILED
    assert [method for method, _ in arm.sent] == ["PUT"], "the delete was sent anyway"
    assert outcome.monthly_saving == 0.0


def test_a_dry_run_and_an_apply_plan_the_same_steps(pricing):
    """--apply gates whether steps are sent, never which steps are planned."""
    dry = clean.plan_for(RecordingArm(), _finding(), pricing)
    live_arm = RecordingArm()
    live = clean.apply_outcome(clean.plan_for(live_arm, _finding(), pricing), live_arm)
    assert [(s.method, s.resource_type) for s in dry.steps] == [
        (s.method, s.resource_type) for s in live.steps
    ]


# --------------------------------------------------------------------------
# the registry-wide contract


def test_every_check_has_a_cleaner_or_a_stated_reason():
    """ "Refuse rather than guess" only works if the refusal explains itself."""
    silent = [
        name for name, spec in CHECKS.items() if name not in CLEANERS and not spec.uncleanable
    ]
    assert silent == [], f"checks with neither a cleaner nor a reason: {silent}"


def test_no_check_both_cleans_and_refuses():
    both = [name for name, spec in CHECKS.items() if name in CLEANERS and spec.uncleanable]
    assert both == [], f"checks that both clean and refuse: {both}"


def test_every_step_a_cleaner_yields_is_a_mutation():
    """A cleaner that yielded a GET would make a dry run look like it did work."""
    for name, plan in CLEANERS.items():
        assert plan.__doc__, f"{name}'s cleaner does not say what it does"


def test_the_audit_document_records_what_was_planned(arm, pricing):
    outcome = clean.plan_for(arm, _finding(), pricing)
    document = clean.audit_document([outcome], applied=False, principal="someone@example.com")
    assert document["mode"] == "dry-run"
    assert document["counts"] == {clean.PLANNED: 1}
    action = document["actions"][0]
    assert action["resource_group"] == "test-rg"
    assert action["subscription"] == SUBSCRIPTION
    assert [step["method"] for step in action["steps"]] == ["PUT", "DELETE"]


# --------------------------------------------------------------------------
# AI Services, Container Apps and ML compute


ACCOUNT_ID = arm_id("Microsoft.CognitiveServices/accounts", "acct-ptu")
DEPLOYMENT_ID = f"{ACCOUNT_ID}/deployments/ptu-idle"
METRICS_PATH = f"{ACCOUNT_ID}/providers/Microsoft.Insights/metrics"


def test_a_deployment_that_started_serving_since_the_scan_is_left_alone(pricing):
    served = {
        "value": [
            {
                "timeseries": [
                    {
                        "metadatavalues": [{"value": "ptu-idle"}],
                        "data": [{"total": 12}],
                    }
                ]
            }
        ]
    }
    arm = RecordingArm({METRICS_PATH: served})
    outcome = clean.plan_for(
        arm,
        _finding(
            check="idle-provisioned-deployment",
            resource="acct-ptu/ptu-idle",
            identifier=DEPLOYMENT_ID,
        ),
        pricing,
    )
    assert outcome.status == clean.FAILED
    assert "has served 12 request(s)" in outcome.error


def test_a_deployment_still_idle_is_deleted(pricing):
    arm = RecordingArm()
    outcome = clean.plan_for(
        arm,
        _finding(
            check="idle-provisioned-deployment",
            resource="acct-ptu/ptu-idle",
            identifier=DEPLOYMENT_ID,
        ),
        pricing,
    )
    assert [step.summary for step in outcome.steps] == [f"DELETE {DEPLOYMENT_ID}"]
    assert METRICS_PATH in arm.reads


EMPTY_ACCOUNT_ID = arm_id("Microsoft.CognitiveServices/accounts", "ai-empty")


def test_an_account_that_gained_a_deployment_is_not_deleted(pricing):
    arm = RecordingArm({f"{EMPTY_ACCOUNT_ID}/deployments": [{"name": "chat"}]})
    outcome = clean.plan_for(
        arm,
        _finding(
            check="empty-ai-services-account", resource="ai-empty", identifier=EMPTY_ACCOUNT_ID
        ),
        pricing,
    )
    assert outcome.status == clean.FAILED
    assert "now has 1 deployments (chat)" in outcome.error


def test_an_empty_account_delete_is_recoverable(pricing):
    """Cognitive Services soft-delete keeps a deleted account for 48 hours."""
    outcome = clean.plan_for(
        RecordingArm(),
        _finding(
            check="empty-ai-services-account", resource="ai-empty", identifier=EMPTY_ACCOUNT_ID
        ),
        pricing,
    )
    assert [step.summary for step in outcome.steps] == [f"DELETE {EMPTY_ACCOUNT_ID}"]
    assert not outcome.steps[0].irreversible


ENVIRONMENT_ID = arm_id("Microsoft.App/managedEnvironments", "env-empty")
APPS_PATH = f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.App/containerApps"


def test_an_environment_that_gained_an_app_is_not_deleted(pricing):
    """Deleting an environment deletes the apps inside it."""
    arm = RecordingArm({APPS_PATH: [{"properties": {"environmentId": ENVIRONMENT_ID.upper()}}]})
    outcome = clean.plan_for(
        arm,
        _finding(
            check="empty-container-apps-environment",
            resource="env-empty",
            identifier=ENVIRONMENT_ID,
        ),
        pricing,
    )
    assert outcome.status == clean.FAILED
    assert "now holds a container app" in outcome.error


def test_deleting_an_environment_is_irreversible_because_its_ip_is_released(pricing):
    outcome = clean.plan_for(
        RecordingArm(),
        _finding(
            check="empty-container-apps-environment",
            resource="env-empty",
            identifier=ENVIRONMENT_ID,
        ),
        pricing,
    )
    assert [step.summary for step in outcome.steps] == [f"DELETE {ENVIRONMENT_ID}"]
    assert outcome.steps[0].irreversible


def test_an_idle_container_app_and_profile_are_refused_with_a_reason(pricing):
    for check in ("idle-container-app", "idle-workload-profile"):
        outcome = clean.plan_for(RecordingArm(), _finding(check=check), pricing)
        assert outcome.status == clean.UNSUPPORTED
        assert "az containerapp" in outcome.error


COMPUTE_ID = (
    arm_id("Microsoft.MachineLearningServices/workspaces", "ws-research") + "/computes/ci-forever"
)


def _compute_finding():
    return _finding(
        check="idle-ml-compute", resource="ws-research/ci-forever", identifier=COMPUTE_ID
    )


def test_a_running_instance_is_stopped_not_deleted(pricing):
    arm = RecordingArm(
        {
            COMPUTE_ID: {
                "properties": {
                    "computeType": "ComputeInstance",
                    "properties": {"state": "Running"},
                }
            }
        }
    )
    outcome = clean.plan_for(arm, _compute_finding(), pricing)
    assert [step.summary for step in outcome.steps] == [f"POST {COMPUTE_ID}/stop"]
    assert not outcome.steps[0].irreversible


def test_an_instance_given_an_idle_shutdown_since_the_scan_is_left_alone(pricing):
    arm = RecordingArm(
        {
            COMPUTE_ID: {
                "properties": {
                    "computeType": "ComputeInstance",
                    "properties": {"state": "Running", "idleTimeBeforeShutdown": "PT30M"},
                }
            }
        }
    )
    outcome = clean.plan_for(arm, _compute_finding(), pricing)
    assert outcome.status == clean.FAILED


def test_a_cluster_minimum_is_dropped_and_its_maximum_kept(pricing):
    arm = RecordingArm(
        {
            COMPUTE_ID: {
                "properties": {
                    "computeType": "AmlCompute",
                    "properties": {"scaleSettings": {"minNodeCount": 2, "maxNodeCount": 6}},
                }
            }
        }
    )
    outcome = clean.plan_for(arm, _compute_finding(), pricing)
    (step,) = outcome.steps
    assert step.method == "PATCH"
    assert step.body == {
        "properties": {"properties": {"scaleSettings": {"minNodeCount": 0, "maxNodeCount": 6}}}
    }
