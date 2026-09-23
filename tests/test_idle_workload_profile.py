"""Dedicated workload profiles that no app runs on."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.empty_container_apps_environment import APP_TYPE, RESOURCE_TYPE
from zombiescan.packs.core.idle_workload_profile import idle_workload_profile

FIXTURE = load_fixture("idle_workload_profile")
D4_MONTH = (4 * 0.057077 + 16 * 0.004978) * 730
MANAGEMENT_MONTH = 0.1 * 730


def _findings(make_context):
    ctx, _ = make_context({RESOURCE_TYPE: FIXTURE["environments"], APP_TYPE: FIXTURE["apps"]})
    return {f.resource_id: f for f in idle_workload_profile(ctx)}


def test_only_unused_profiles_with_standing_instances_are_reported(make_context):
    """e8-used runs an app, d8-zero scales to nothing, and env-empty is the
    other check's finding."""
    assert set(_findings(make_context)) == {
        "env-mixed/d4-idle",
        "env-mixed/gpu-idle",
        "env-single/d4-solo",
    }


def test_a_profile_among_several_is_priced_at_its_instances(make_context):
    finding = _findings(make_context)["env-mixed/d4-idle"]
    assert finding.monthly_cost == D4_MONTH
    assert not finding.details["includes_management_fee"]


def test_the_only_dedicated_profile_also_carries_the_management_fee(make_context):
    finding = _findings(make_context)["env-single/d4-solo"]
    assert finding.monthly_cost == 2 * D4_MONTH + MANAGEMENT_MONTH
    assert finding.details["includes_management_fee"]


def test_a_gpu_profile_is_unpriced_rather_than_guessed(make_context):
    finding = _findings(make_context)["env-mixed/gpu-idle"]
    assert finding.monthly_cost == 0.0
    assert finding.approximate_cost
