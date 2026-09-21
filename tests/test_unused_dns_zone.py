from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_dns_zone import unused_dns_zone

FIXTURE = load_fixture("unused_dns_zone")


def _context(make_context, zones=None):
    listing = zones if zones is not None else FIXTURE["zones"]
    return make_context(
        {
            "managedZones.list": listing,
            "resourceRecordSets.list": lambda **kw: FIXTURE["records"][kw["managedZone"]],
        }
    )


@pytest.fixture
def findings(make_context):
    ctx, _ = _context(make_context)
    return {f.resource_id: f for f in unused_dns_zone(ctx)}


def test_a_zone_holding_only_soa_and_ns_is_flagged(findings):
    """Those two cannot be deleted, so a zone with only them publishes nothing."""
    assert set(findings) == {"agent-local", "empty-public"}


def test_a_zone_with_a_real_record_is_left_alone(findings):
    assert "example-com" not in findings


def test_the_visibility_is_reported(findings):
    assert findings["agent-local"].details["visibility"] == "private"
    assert findings["empty-public"].details["visibility"] == "public"
    assert findings["agent-local"].reason.startswith("Private zone 'agent.local.'")


def test_a_zone_is_priced_at_the_first_tier_in_a_small_project(findings):
    assert findings["agent-local"].monthly_cost == pytest.approx(0.20)


def test_pricing_is_marginal_not_average(make_context):
    """Removing one zone from a project with thirty saves the second tier's rate,
    not the headline one."""
    many = {
        "managedZones": [
            {"name": "agent-local", "dnsName": "agent.local.", "visibility": "private"}
        ]
        + [
            {"name": f"filler-{i}", "dnsName": f"f{i}.example.", "visibility": "public"}
            for i in range(29)
        ]
    }
    records = dict(FIXTURE["records"])
    for i in range(29):
        records[f"filler-{i}"] = {"rrsets": [{"type": "A"}]}
    ctx, _ = make_context(
        {
            "managedZones.list": many,
            "resourceRecordSets.list": lambda **kw: records[kw["managedZone"]],
        }
    )
    findings = {f.resource_id: f for f in unused_dns_zone(ctx)}
    assert findings["agent-local"].monthly_cost == pytest.approx(0.10)
    assert findings["agent-local"].details["zones_in_project"] == 30


def test_the_note_explains_the_marginal_rate(findings):
    assert "marginal rate" in findings["agent-local"].details["note"]


def test_the_record_types_found_are_reported(findings):
    assert findings["agent-local"].details["record_types"] == ["NS", "SOA"]


def test_zones_are_global(findings):
    assert all(f.location == "global" for f in findings.values())
