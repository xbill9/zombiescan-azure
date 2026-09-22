"""NAT gateways with no subnet behind them."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.idle_nat_gateway import RESOURCE_TYPE, idle_nat_gateway


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("idle_nat_gateway")})
    return list(idle_nat_gateway(ctx)), arm


def test_a_gateway_with_a_subnet_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"egress-gw-old"}


def test_an_idle_nat_gateway_costs_the_full_hourly_fee(make_context):
    """The Azure answer, and the reverse of Cloud NAT.

    Google bills gateway uptime per VM behind it, so an idle Cloud NAT is
    nearly free. Azure bills a flat hourly fee from creation to deletion.
    """
    findings, _ = _findings(make_context)
    gateway = findings[0]
    assert gateway.monthly_cost == 0.045 * 730
    assert "whether or not anything routes through it" in gateway.reason


def test_the_rate_is_global_so_a_region_does_not_change_it(make_context, pricing):
    """Azure publishes this against armRegionName "Global"."""
    eastus, _ = pricing.rate("nat_gateway.month")
    assert eastus == 0.045 * 730


def test_the_addresses_it_holds_are_left_to_their_own_check(make_context):
    findings, _ = _findings(make_context)
    gateway = findings[0]
    assert gateway.details["public_ip_addresses"] == ["nat-ip-1", "nat-ip-2"]
    assert "unused-public-ip" in gateway.details["note"]
