from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.orphaned_snapshot import orphaned_snapshot

FIXTURE = load_fixture("orphaned_snapshot")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "disks.aggregatedList": FIXTURE["disks"],
            "snapshots.list": FIXTURE["snapshots"],
        }
    )
    return {f.resource_id: f for f in orphaned_snapshot(ctx)}


def test_a_snapshot_of_a_deleted_disk_is_flagged(findings):
    assert "backup-of-deleted-db" in findings


def test_a_snapshot_of_a_live_disk_is_left_alone(findings):
    assert "backup-of-live-disk" not in findings


def test_matching_is_by_disk_id_not_by_name(findings):
    """A disk deleted and recreated under the same name is a different disk,
    and the old snapshots really are orphaned."""
    assert "recreated-name-collision" in findings


def test_a_snapshot_with_no_recorded_source_is_not_assumed_orphaned(findings):
    """An imported snapshot says nothing about a disk, so nothing can be concluded."""
    assert "imported-snapshot-no-source" not in findings


def test_cost_is_the_stored_bytes_not_the_source_disk_size(findings):
    """Snapshots are compressed and incremental; diskSizeGb would overstate it."""
    # 10 GiB stored at the us-central1 rate of 0.05
    assert findings["backup-of-deleted-db"].monthly_cost == pytest.approx(0.5)
    assert findings["backup-of-deleted-db"].details["stored_gb"] == 10.0
    assert findings["backup-of-deleted-db"].details["disk_size_gb"] == 200.0


def test_a_multi_region_snapshot_falls_back_and_is_marked(findings):
    """ "us" is a multi-region and has no entry in a region-keyed table."""
    orphan = findings["multi-region-orphan"]
    assert orphan.approximate_cost is True
    assert orphan.location == "us"


def test_the_location_is_where_the_bytes_are_stored(findings):
    assert findings["backup-of-deleted-db"].location == "us-central1"


def test_the_delete_command_takes_no_location_flag(findings):
    """Snapshots are global resources; a --zone would be rejected."""
    remediation = findings["backup-of-deleted-db"].remediation
    assert remediation == (
        "gcloud compute snapshots delete backup-of-deleted-db --project=test-project --quiet"
    )


def test_the_source_disk_is_named_in_the_reason(findings):
    assert "old-db" in findings["backup-of-deleted-db"].reason
