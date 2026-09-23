"""Container Apps environments with no app in them."""

from __future__ import annotations

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.empty_container_apps_environment import (
    APP_TYPE,
    RESOURCE_TYPE,
    empty_container_apps_environment,
    instance_size,
)

FIXTURE = load_fixture("empty_container_apps_environment")
D4_MONTH = (4 * 0.057077 + 16 * 0.004978) * 730
MANAGEMENT_MONTH = 0.1 * 730


def _findings(make_context):
    ctx, _ = make_context({RESOURCE_TYPE: FIXTURE["environments"], APP_TYPE: FIXTURE["apps"]})
    return {f.resource_id: f for f in empty_container_apps_environment(ctx)}


def test_an_environment_holding_an_app_is_not_reported(make_context):
    assert set(_findings(make_context)) == {"env-empty", "env-empty-dedicated"}


def test_an_empty_consumption_environment_costs_nothing(make_context):
    finding = _findings(make_context)["env-empty"]
    assert finding.monthly_cost == 0.0
    assert "costs nothing" in finding.reason


def test_an_empty_dedicated_environment_pays_its_instances_and_the_fee(make_context):
    finding = _findings(make_context)["env-empty-dedicated"]
    assert finding.monthly_cost == D4_MONTH + MANAGEMENT_MONTH


def test_instance_sizes_follow_the_profile_name():
    """D is 4 GiB per vCPU, E is 8, DC is 4; GPU profiles are not sized."""
    assert instance_size("D4") == (4, 16)
    assert instance_size("E32") == (32, 256)
    assert instance_size("DC8") == (8, 32)
    assert instance_size("NC24-A100") is None


def test_the_delete_command_takes_yes(make_context):
    finding = _findings(make_context)["env-empty"]
    assert finding.remediation == (
        f"az containerapp env delete --name env-empty --resource-group test-rg "
        f"--subscription {SUBSCRIPTION} --yes"
    )
