from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unattached_disk import unattached_disk


def _run(make_context):
    ctx, client = make_context({"disks.aggregatedList": load_fixture("unattached_disk")})
    return {f.resource_id: f for f in unattached_disk(ctx)}, client


@pytest.fixture
def findings(make_context):
    return _run(make_context)[0]


def test_flags_every_disk_with_no_users(findings):
    assert set(findings) == {
        "orphaned-build-cache",
        "big-standard-scratch",
        "fast-hyperdisk",
        "future-disk-type",
        "eu-leftover",
        "unpriced-region-disk",
    }


def test_attached_disk_is_not_flagged(findings):
    assert "attached-boot-disk" not in findings


def test_one_aggregated_call_covers_every_zone(make_context):
    """The check must not fan out per zone: that is the whole point of aggregatedList."""
    _, client = _run(make_context)
    assert [op for op, _ in client.call_log] == ["disks.aggregatedList"]
    assert client.calls["disks.aggregatedList"] == {"project": "test-project"}


def test_a_scope_holding_only_a_warning_is_skipped(findings):
    """Compute reports an empty zone as a warning, not as an empty list."""
    assert not any(f.location == "us-west1-a" for f in findings.values())


def test_location_comes_from_the_aggregation_scope(findings):
    assert findings["orphaned-build-cache"].location == "us-central1-a"
    assert findings["big-standard-scratch"].location == "us-central1-b"
    assert findings["eu-leftover"].location == "europe-west1-b"


@pytest.mark.parametrize(
    ("disk", "expected"),
    [
        ("orphaned-build-cache", 100 * 0.10),
        ("big-standard-scratch", 500 * 0.04),
        ("fast-hyperdisk", 200 * 0.08),
    ],
)
def test_cost_is_size_times_the_rate_for_that_disk_type(findings, disk, expected):
    assert findings[disk].monthly_cost == pytest.approx(expected)


def test_a_zone_prices_as_its_region(findings):
    """Pricing is regional, so us-central1-b must find the us-central1 rate."""
    assert findings["big-standard-scratch"].approximate_cost is False
    eu = findings["eu-leftover"]
    assert eu.monthly_cost == pytest.approx(100 * 0.11)
    assert eu.approximate_cost is False


def test_unpriced_region_falls_back_and_is_marked(findings):
    disk = findings["unpriced-region-disk"]
    assert disk.monthly_cost == pytest.approx(10 * 0.10)  # us-central1 fallback
    assert disk.approximate_cost is True


def test_unknown_disk_type_is_priced_as_balanced_and_marked(findings):
    unknown = findings["future-disk-type"]
    assert unknown.monthly_cost == pytest.approx(8 * 0.10)
    assert unknown.approximate_cost is True


def test_hyperdisk_understatement_is_disclosed(findings):
    assert "IOPS" in findings["fast-hyperdisk"].details["note"]
    assert "note" not in findings["orphaned-build-cache"].details


def test_labels_are_surfaced(findings):
    assert findings["orphaned-build-cache"].details["labels"] == {"team": "platform"}
    assert findings["big-standard-scratch"].details["labels"] == {}


def test_remediation_snapshots_first_and_names_the_zone(findings):
    remediation = findings["orphaned-build-cache"].remediation
    assert remediation.startswith("gcloud compute disks snapshot orphaned-build-cache")
    assert remediation.index("snapshot") < remediation.index("delete")
    assert "--zone=us-central1-a" in remediation
    assert "--project=test-project" in remediation
    assert remediation.count("--quiet") == 2


def test_no_mutating_calls_are_possible(findings):
    """Remediation is a string. Nothing in the finding can execute it."""
    for finding in findings.values():
        assert isinstance(finding.remediation, str)
