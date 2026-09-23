"""Load balancers with nothing behind them."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.idle_load_balancer import RESOURCE_TYPE, idle_load_balancer


def _findings(make_context):
    ctx, arm = make_context(
        {
            RESOURCE_TYPE: load_fixture("idle_load_balancer"),
            "Microsoft.Network/publicIPAddresses": load_fixture("held_public_ips"),
        }
    )
    return list(idle_load_balancer(ctx)), arm


def test_a_balancer_with_a_populated_pool_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"api-lb", "legacy-lb"}


def test_an_empty_pool_and_no_pool_at_all_are_told_apart(make_context):
    findings, _ = _findings(make_context)
    empty = next(f for f in findings if f.resource_id == "api-lb")
    none = next(f for f in findings if f.resource_id == "legacy-lb")
    assert "nothing in any of them" in empty.reason
    assert "no backend pool at all" in none.reason


def test_only_the_standard_sku_carries_a_charge(make_context):
    findings, _ = _findings(make_context)
    standard = next(f for f in findings if f.resource_id == "api-lb")
    basic = next(f for f in findings if f.resource_id == "legacy-lb")
    assert standard.monthly_cost == 0.025 * 730 + 0.005 * 730
    assert basic.monthly_cost == 0.0
    assert "free but retired" in basic.reason


def test_frontend_public_ips_are_priced_with_the_balancer(make_context):
    """Each frontend address names the balancer, so unused-public-ip skips it."""
    findings, _ = _findings(make_context)
    api = next(f for f in findings if f.resource_id == "api-lb")
    assert [ip["name"] for ip in api.details["public_ips"]] == ["api-lb-ip"]
    assert "frontend public IP(s) add $3.65/month" in api.reason


def test_the_figure_excludes_data_processing(make_context):
    findings, _ = _findings(make_context)
    assert "data processed is not included" in findings[0].details["note"]
