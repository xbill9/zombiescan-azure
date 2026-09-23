"""Container apps kept alive by minReplicas and serving nothing."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.idle_container_app import (
    ENVIRONMENT_TYPE,
    RESOURCE_TYPE,
    gib,
    idle_container_app,
)

FIXTURE = load_fixture("idle_container_app")


def _findings(make_context):
    ctx, arm = make_context(
        {
            RESOURCE_TYPE: FIXTURE["apps"],
            ENVIRONMENT_TYPE: FIXTURE["environments"],
            "containerApps/app-idle/providers/Microsoft.Insights/metrics": FIXTURE["idle_requests"],
            "containerApps/app-busy/providers/Microsoft.Insights/metrics": FIXTURE["busy_requests"],
        }
    )
    return {f.resource_id: f for f in idle_container_app(ctx)}, arm


def test_only_an_always_on_app_with_no_requests_is_reported(make_context):
    findings, _ = _findings(make_context)
    assert set(findings) == {"app-idle"}


def test_apps_that_cannot_be_idle_waste_are_not_asked_for_metrics(make_context):
    """minReplicas 0 costs nothing idle, a worker has no requests to count, and
    an app on a Dedicated profile is paid for by the profile."""
    _, arm = _findings(make_context)
    asked = {
        path.split("/containerApps/")[1].split("/")[0] for path in arm.calls if "/metrics" in path
    }
    assert asked == {"app-idle", "app-busy"}


def test_the_idle_replica_is_priced_at_the_idle_rates(make_context):
    findings, _ = _findings(make_context)
    idle = findings["app-idle"]
    assert idle.monthly_cost == 1 * (0.5 * 0.0108 * 730 + 1.0 * 0.0108 * 730)
    assert idle.details["min_replicas"] == 1


def test_memory_units_are_read_as_gib():
    assert gib("1Gi") == 1.0
    assert gib("1.5Gi") == 1.5
    assert gib("512Mi") == 0.5
    assert gib(None) == 0.0


def test_the_metrics_provider_is_declared():
    from zombiescan.registry import CHECKS

    assert "Microsoft.Insights" in CHECKS["idle-container-app"].providers
