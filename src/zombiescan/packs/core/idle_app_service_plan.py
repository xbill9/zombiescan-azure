"""App Service plans hosting no apps.

An App Service plan is the thing that is billed, not the app. The plan
reserves its instances -- a P1v3 is about $230 a month, and a plan is often
scaled to two or three of them -- and it keeps reserving them after the last
app on it is deleted. Nothing in the portal's App Service list shows a plan
with no apps, because that list shows apps.

This is the most expensive single finding a small subscription is likely to
carry, and the easiest to create: deleting a web app never deletes its plan.

Free, Shared and Dynamic (consumption) plans are reported at no cost, because
they genuinely have none.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-app-service-plan"

RESOURCE_TYPE = "Microsoft.Web/serverfarms"

# Tiers with no reserved instance to pay for. A consumption Function plan
# bills per execution and a Free plan bills nothing at all.
FREE_TIERS = frozenset({"Free", "Shared", "Dynamic", "FlexConsumption"})


def build_finding(ctx: ScanContext, plan: dict[str, Any]) -> Finding:
    name = plan["name"]
    arm_id = plan.get("id") or ""
    group = plan.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(plan)
    properties = helpers.properties(plan)
    sku = plan.get("sku") or {}

    tier = sku.get("tier") or ""
    sku_name = sku.get("name") or ""
    capacity = int(sku.get("capacity") or 1)
    # `reserved` is how ARM spells "this is a Linux plan", and Azure prices
    # Linux plans at about half the Windows rate for the same SKU.
    linux = bool(properties.get("reserved"))
    variant = f"{sku_name} linux" if linux else sku_name

    if tier in FREE_TIERS:
        price, approximate, cost = 0.0, False, 0.0
    else:
        price, approximate = ctx.pricing.rate(
            "app_service.month", region=azure.region_of(location), variant=variant
        )
        cost = price * capacity

    platform = "Linux" if linux else "Windows"
    if cost:
        reason = (
            f"App Service plan hosts no apps but keeps {capacity} {sku_name} "
            f"{platform} instance(s) reserved and billed"
        )
    else:
        reason = (
            f"App Service plan hosts no apps. The {tier or sku_name} tier reserves no "
            f"instance, so this costs nothing today"
        )

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="app-service-plan",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=helpers.az(
            f"az appservice plan delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate and bool(cost),
        details={
            "sku": sku_name,
            "tier": tier,
            "capacity": capacity,
            "platform": platform,
            "zone_redundant": properties.get("zoneRedundant"),
            "tags": plan.get("tags") or {},
            "usd_per_instance_month": round(price, 2),
            "note": (
                "a plan is billed for its reserved instances whether or not an app "
                "runs on it; deleting the last app on a plan does not delete the plan"
            ),
        },
    )


@check(CHECK_NAME, "App Service plans hosting no apps", providers="Microsoft.Web")
def idle_app_service_plan(ctx: ScanContext) -> Iterator[Finding]:
    for plan in ctx.list(RESOURCE_TYPE):
        if int(helpers.properties(plan).get("numberOfSites") or 0) == 0:
            yield build_finding(ctx, plan)
