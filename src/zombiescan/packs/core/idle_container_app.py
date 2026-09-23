"""Container apps kept running by ``minReplicas`` that served no requests in a week.

A Consumption container app with ``minReplicas: 0`` scales to nothing and
costs nothing idle. One with ``minReplicas`` of 1 or more keeps that many
replicas alive around the clock, and a replica that is running but not
serving bills at the idle vCPU and memory rates -- small per replica, but
charged every second of every month.

Idle means no requests in ``helpers.LOOKBACK_DAYS``, read from the app's
``Requests`` metric. A worker app with no ingress processes queue messages
rather than requests and is skipped, because a zero there says nothing.

Apps on a Dedicated workload profile are skipped too: their cost is the
profile's instances, which ``idle-workload-profile`` prices.

The monthly free grant for Consumption usage is per subscription and is not
subtracted.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.packs.core.empty_container_apps_environment import environment_of
from zombiescan.registry import check

CHECK_NAME = "idle-container-app"

RESOURCE_TYPE = "Microsoft.App/containerApps"
ENVIRONMENT_TYPE = "Microsoft.App/managedEnvironments"

METRIC = "Requests"


def gib(memory: Any) -> float:
    """``"1Gi"``, ``"1.5Gi"``, ``"512Mi"`` as GiB."""
    match = re.fullmatch(r"\s*([\d.]+)\s*(Gi|Mi)?\s*", str(memory or ""))
    if not match:
        return 0.0
    value = float(match.group(1))
    return value / 1024 if match.group(2) == "Mi" else value


def replica_size(app: dict[str, Any]) -> tuple[float, float]:
    """``(vCPU, GiB)`` one replica reserves: the sum over its containers."""
    containers = (helpers.properties(app).get("template") or {}).get("containers") or []
    resources = [container.get("resources") or {} for container in containers]
    return (
        sum(float(r.get("cpu") or 0) for r in resources),
        sum(gib(r.get("memory")) for r in resources),
    )


def consumption_profiles(environments: Iterator[dict[str, Any]]) -> dict[str, set[str]]:
    """Each environment's Consumption profile names, keyed by lowercased id."""
    found: dict[str, set[str]] = {}
    for environment in environments:
        profiles = helpers.properties(environment).get("workloadProfiles") or []
        found[str(environment.get("id") or "").lower()] = {
            str(p.get("name") or "").lower()
            for p in profiles
            if str(p.get("workloadProfileType") or "").startswith("Consumption")
        }
    return found


def build_finding(ctx: ScanContext, app: dict[str, Any], replicas: int) -> Finding:
    name = app["name"]
    arm_id = app.get("id") or ""
    group = app.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(app)
    region = azure.region_of(location)
    vcpu, memory = replica_size(app)

    vcpu_rate, approximate = ctx.pricing.rate(
        "container_apps.month", region=region, variant="idle_vcpu"
    )
    gib_rate, gib_approximate = ctx.pricing.rate(
        "container_apps.month", region=region, variant="idle_gib"
    )
    cost = replicas * (vcpu * vcpu_rate + memory * gib_rate)

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="container-app",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"minReplicas keeps {replicas} replica(s) of {vcpu:g} vCPU / {memory:g} GiB running, "
            f"and the app served no requests in {helpers.LOOKBACK_DAYS} days"
        ),
        monthly_cost=cost,
        remediation=helpers.az(
            f"az containerapp update --name {helpers.arg(name)} --min-replicas 0",
            ctx.subscription,
            group,
        ),
        approximate_cost=approximate or gib_approximate,
        details={
            "min_replicas": replicas,
            "vcpu_per_replica": vcpu,
            "gib_per_replica": memory,
            "environment": azure.name_of(environment_of(app)),
            "requests": 0,
            "lookback_days": helpers.LOOKBACK_DAYS,
            "tags": app.get("tags") or {},
            "note": (
                "idle Consumption rate for the replicas minReplicas keeps alive, before the "
                "per-subscription free grant. With minReplicas 0 the first request after a "
                "quiet spell waits for a cold start"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Always-on container apps serving nothing",
    providers=("Microsoft.App", "Microsoft.Insights"),
    uncleanable=(
        "lowering minReplicas creates a new revision and trades the idle charge for a "
        "cold start on the next request; run the printed 'az containerapp update' "
        "once that trade is decided"
    ),
)
def idle_container_app(ctx: ScanContext) -> Iterator[Finding]:
    candidates = []
    for app in ctx.list(RESOURCE_TYPE):
        properties = helpers.properties(app)
        replicas = int(
            ((properties.get("template") or {}).get("scale") or {}).get("minReplicas") or 0
        )
        ingress = (properties.get("configuration") or {}).get("ingress")
        if replicas > 0 and ingress and app.get("id"):
            candidates.append((app, replicas))
    if not candidates:
        return
    consumption = consumption_profiles(ctx.list(ENVIRONMENT_TYPE))
    for app, replicas in candidates:
        profile = str(helpers.properties(app).get("workloadProfileName") or "").lower()
        # No profile name means an environment with Consumption only.
        if profile and profile not in consumption.get(environment_of(app).lower(), set()):
            continue
        if not helpers.metric_totals(ctx, app["id"], METRIC).get(""):
            yield build_finding(ctx, app, replicas)
