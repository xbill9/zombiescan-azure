"""DNS zones holding only the records Azure created."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan import azure
from zombiescan.packs.core.unused_dns_zone import RESOURCE_TYPE, unused_dns_zone


def _findings(make_context, fixture=None):
    ctx, arm = make_context({RESOURCE_TYPE: fixture or load_fixture("unused_dns_zone")})
    return list(unused_dns_zone(ctx)), arm


def test_a_zone_with_records_in_it_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"old-campaign.example", "staging.example"}


def test_an_empty_zone_costs_the_first_tier_rate(make_context):
    findings, _ = _findings(make_context)
    assert all(f.monthly_cost == 0.50 for f in findings)
    assert findings[0].details["priced_at_tier"] == "first 25"


def test_a_subscription_past_the_first_tier_saves_the_lower_rate(make_context):
    """Deleting a zone saves what the tier you are actually in charges."""
    zones = load_fixture("unused_dns_zone")
    template = zones["value"][0]
    zones["value"] = [
        {**template, "name": f"zone-{n}.example", "id": template["id"] + str(n)} for n in range(30)
    ]
    findings, _ = _findings(make_context, zones)
    assert len(findings) == 30
    assert all(f.monthly_cost == 0.10 for f in findings)
    assert findings[0].details["priced_at_tier"] == "beyond the first 25"


def test_a_zone_has_no_region(make_context):
    """Azure hosts a public zone on anycast name servers and prices it globally."""
    findings, _ = _findings(make_context)
    assert all(f.location == azure.GLOBAL for f in findings)
