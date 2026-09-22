"""App Service plans hosting no apps."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.idle_app_service_plan import RESOURCE_TYPE, idle_app_service_plan


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("idle_app_service_plan")})
    return list(idle_app_service_plan(ctx)), arm


def test_a_plan_with_apps_on_it_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "busy-plan" not in {f.resource_id for f in findings}


def test_the_cost_is_the_rate_times_the_instance_count(make_context):
    """A plan reserves every instance it is scaled to, app or no app."""
    findings, _ = _findings(make_context)
    plan = next(f for f in findings if f.resource_id == "prod-plan")
    assert plan.details["capacity"] == 2
    assert plan.monthly_cost == 0.30 * 730 * 2


def test_linux_and_windows_are_priced_differently(make_context):
    """ARM spells "this is Linux" as `reserved`, and Azure charges about half."""
    findings, _ = _findings(make_context)
    linux = next(f for f in findings if f.resource_id == "linux-plan")
    assert linux.details["platform"] == "Linux"
    assert linux.monthly_cost == 0.15 * 730


def test_a_free_tier_plan_reserves_nothing_and_says_so(make_context):
    findings, _ = _findings(make_context)
    free = next(f for f in findings if f.resource_id == "free-plan")
    assert free.monthly_cost == 0.0
    assert "costs nothing today" in free.reason


def test_the_finding_says_that_deleting_an_app_leaves_the_plan(make_context):
    findings, _ = _findings(make_context)
    plan = next(f for f in findings if f.resource_id == "prod-plan")
    assert "does not delete the plan" in plan.details["note"]
