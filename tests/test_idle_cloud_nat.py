from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.idle_cloud_nat import idle_cloud_nat

FIXTURE = load_fixture("idle_cloud_nat")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "routers.aggregatedList": FIXTURE["routers"],
            "instances.aggregatedList": FIXTURE["instances"],
        }
    )
    return {f.resource_id: f for f in idle_cloud_nat(ctx)}


def test_a_nat_in_a_network_with_no_instances_is_flagged(findings):
    assert set(findings) == {"idle-router/abandoned-nat", "idle-router/auto-ip-nat"}


def test_a_nat_serving_running_instances_is_left_alone(findings):
    assert "busy-router/prod-nat" not in findings


def test_a_router_with_no_nat_at_all_is_not_a_finding(findings):
    """A Cloud Router without a NAT config costs nothing and does nothing here."""
    assert not any(key.startswith("router-with-no-nat") for key in findings)


def test_cost_is_the_reserved_addresses_not_the_gateway(findings):
    """Google bills NAT uptime per VM using it, so an idle gateway's uptime is free.
    Unlike an AWS NAT gateway, which bills a flat hourly charge regardless."""
    # two reserved addresses at 0.005/hour over 730 hours
    assert findings["idle-router/abandoned-nat"].monthly_cost == pytest.approx(2 * 0.005 * 730)


def test_an_auto_allocated_nat_costs_nothing_while_idle(findings):
    finding = findings["idle-router/auto-ip-nat"]
    assert finding.monthly_cost == 0.0
    assert "auto-allocated" in finding.reason


def test_the_note_explains_why_uptime_is_not_charged(findings):
    note = findings["idle-router/abandoned-nat"].details["note"]
    assert "billed per VM using it" in note


def test_the_reserved_addresses_are_listed_as_bare_names(findings):
    assert findings["idle-router/abandoned-nat"].details["reserved_addresses"] == [
        "nat-ip-1",
        "nat-ip-2",
    ]


def test_removal_names_the_router_and_the_region(findings):
    """There is no delete call for a NAT; it is addressed through its router."""
    remediation = findings["idle-router/abandoned-nat"].remediation
    assert remediation == (
        "gcloud compute routers nats delete abandoned-nat --router=idle-router "
        "--region=us-central1 --project=test-project --quiet"
    )
