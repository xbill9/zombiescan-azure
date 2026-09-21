from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.stopped_sql_instance import stopped_sql_instance


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context({"instances.list": load_fixture("stopped_sql_instance")})
    return {f.resource_id: f for f in stopped_sql_instance(ctx)}


def test_a_stopped_instance_is_flagged(findings):
    assert "old-reporting-db" in findings


def test_a_running_instance_is_left_alone(findings):
    assert "live-prod-db" not in findings


def test_an_activation_policy_of_never_counts_as_stopped(findings):
    """That is what `gcloud sql instances patch --activation-policy=NEVER` sets,
    and the reported state can lag behind it."""
    assert "ha-staging-db" in findings


def test_cost_is_the_storage_at_its_own_class_rate(findings):
    # 500 GB PD_SSD in us-central1 at 0.17
    assert findings["old-reporting-db"].monthly_cost == pytest.approx(500 * 0.17)
    # 200 GB PD_HDD in us-central1 at 0.09
    assert findings["cheap-hdd-db"].monthly_cost == pytest.approx(200 * 0.09)


def test_a_high_availability_instance_bills_its_storage_twice(findings):
    """A regional instance keeps a standby replica, and its storage bills too."""
    finding = findings["ha-staging-db"]
    assert finding.monthly_cost == pytest.approx(100 * 0.187 * 2)
    assert finding.details["high_availability"] is True
    assert "twice over" in finding.reason


def test_the_note_says_what_stops_and_what_does_not(findings):
    note = findings["old-reporting-db"].details["note"]
    assert "vCPU and memory stop when the instance does" in note


def test_the_location_is_the_instance_region(findings):
    assert findings["old-reporting-db"].location == "us-central1"
    assert findings["ha-staging-db"].location == "europe-west1"


def test_one_list_call_covers_every_region(make_context):
    """Cloud SQL lists a whole project at once, so there is no location loop."""
    ctx, client = make_context({"instances.list": load_fixture("stopped_sql_instance")})
    list(stopped_sql_instance(ctx))
    assert [op for op, _ in client.call_log] == ["instances.list"]
    assert client.calls["instances.list"] == {"project": "test-project"}
