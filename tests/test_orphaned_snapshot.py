"""Snapshots whose source disk is gone."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.orphaned_snapshot import orphaned_snapshot

FIXTURE = load_fixture("orphaned_snapshot")


def _findings(make_context):
    ctx, arm = make_context(
        {
            "Microsoft.Compute/snapshots": FIXTURE["snapshots"],
            "Microsoft.Compute/disks": FIXTURE["disks"],
        }
    )
    return list(orphaned_snapshot(ctx)), arm


def test_only_snapshots_whose_source_is_gone_are_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"pre-upgrade-2025"}


def test_a_snapshot_of_a_live_disk_is_not_waste(make_context):
    findings, _ = _findings(make_context)
    assert "nightly-web01" not in {f.resource_id for f in findings}


def test_a_snapshot_built_from_a_blob_has_no_source_disk_to_outlive(make_context):
    """`createOption: Import` means there was never a disk behind it."""
    findings, _ = _findings(make_context)
    assert "from-vhd" not in {f.resource_id for f in findings}


def test_the_cost_is_the_source_size_and_says_it_is_a_ceiling(make_context):
    findings, _ = _findings(make_context)
    snapshot = findings[0]
    assert snapshot.monthly_cost == 512 * 0.05
    assert "upper bound" in snapshot.details["note"]
    assert "incremental" in snapshot.details["note"]


def test_the_command_takes_no_location_flag(make_context):
    """`az snapshot delete` is addressed by group and name, not by region."""
    findings, _ = _findings(make_context)
    assert "--location" not in findings[0].remediation
    assert "--resource-group test-rg" in findings[0].remediation
