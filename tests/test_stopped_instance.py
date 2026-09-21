from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.stopped_instance import stopped_instance

FIXTURE = load_fixture("stopped_instance")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "disks.aggregatedList": FIXTURE["disks"],
            "instances.aggregatedList": FIXTURE["instances"],
        }
    )
    return {f.resource_id: f for f in stopped_instance(ctx)}


def test_terminated_and_suspended_instances_are_both_flagged(findings):
    assert set(findings) == {"old-batch-runner", "paused-dev-box"}


def test_a_running_instance_is_not_waste(findings):
    assert "web-1" not in findings


def test_cost_is_the_attached_disks_not_the_machine_type(findings):
    """vCPU and memory stop when the instance does; the disks do not."""
    # 50 GB pd-balanced at 0.10 + 500 GB pd-ssd at 0.17
    assert findings["old-batch-runner"].monthly_cost == pytest.approx(50 * 0.10 + 500 * 0.17)


def test_a_local_ssd_with_no_source_disk_is_skipped_not_crashed_on(findings):
    names = [d["name"] for d in findings["old-batch-runner"].details["disks"]]
    assert names == ["stopped-vm-boot", "stopped-vm-data"]


def test_the_note_says_what_the_figure_covers(findings):
    note = findings["old-batch-runner"].details["note"]
    assert "vCPU and memory are not billed while stopped" in note


def test_the_machine_type_is_reported_as_a_bare_name(findings):
    assert findings["old-batch-runner"].details["machine_type"] == "n2-standard-8"


def test_how_long_it_has_been_stopped_is_surfaced(findings):
    assert findings["old-batch-runner"].details["stopped_days_ago"] > 0
    assert findings["paused-dev-box"].details["stopped_days_ago"] is None


def test_the_remediation_keeps_the_disks(findings):
    """Deleting the instance is recoverable only while its disks survive."""
    remediation = findings["old-batch-runner"].remediation
    assert "--keep-disks=all" in remediation
    assert "--zone=us-central1-a" in remediation


def test_the_status_is_reported_so_suspended_is_distinguishable(findings):
    assert findings["old-batch-runner"].details["status"] == "TERMINATED"
    assert findings["paused-dev-box"].details["status"] == "SUSPENDED"
