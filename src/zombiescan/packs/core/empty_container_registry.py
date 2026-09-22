"""Container registries holding no images.

Azure Container Registry bills a flat daily fee by tier -- Basic about $5 a
month, Standard $20, Premium $50 -- with storage included up to the tier's
limit. The fee has nothing to do with what is in the registry, so **an empty
Premium registry costs exactly what a full one does**.

That is the opposite shape to Artifact Registry on Google Cloud, which charges
per gigabyte stored and nothing for the registry itself. There, an empty
repository is free and a large one is the problem. Here the registry is the
line item and its contents are not.

A registry with zero stored bytes is one nobody has ever pushed to, or one
whose images were all deleted. Either way the tier fee is being paid for
nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.azure import ArmError
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "empty-container-registry"

RESOURCE_TYPE = "Microsoft.ContainerRegistry/registries"

SIZE_USAGE = "Size"


def _stored_bytes(ctx: ScanContext, registry_id: str) -> float | None:
    """How many bytes a registry holds, or None if it will not say.

    ``listUsages`` is the only management-plane call that reports it -- the
    registry resource itself carries no size -- and it is a read. A registry
    behind a private endpoint can refuse it, and a refusal has to read as "not
    known" rather than as "empty", or a firewalled registry would be reported
    as waste on the strength of a failed call.
    """
    try:
        usages = ctx.arm.get(f"{registry_id}/listUsages", RESOURCE_TYPE).get("value") or []
    except ArmError:
        return None
    for usage in usages:
        if usage.get("name") == SIZE_USAGE:
            return helpers.gb(usage.get("currentValue"))
    return None


def build_finding(ctx: ScanContext, registry: dict[str, Any]) -> Finding:
    name = registry["name"]
    arm_id = registry.get("id") or ""
    group = registry.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(registry)
    properties = helpers.properties(registry)

    sku = (registry.get("sku") or {}).get("name") or "Basic"
    price, approximate = ctx.pricing.rate("acr.registry_month", sku=sku)
    age = helpers.age_days(properties.get("creationDate"))

    reason = (
        f"{sku} container registry holds no images, and Azure charges its tier fee "
        f"per day regardless of what is in it"
    )
    if age is not None:
        reason += f"; created {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="container-registry",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=price,
        remediation=helpers.az(
            f"az acr delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate,
        details={
            "sku": sku,
            "stored_gb": 0.0,
            "age_days": age,
            "login_server": properties.get("loginServer"),
            "admin_user_enabled": properties.get("adminUserEnabled"),
            "replications": properties.get("dataEndpointHostNames") or [],
            "tags": registry.get("tags") or {},
            "note": (
                "tier fee only. Storage beyond the tier's included allowance is billed "
                "on top, which an empty registry does not reach"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Container registries holding no images",
    providers="Microsoft.ContainerRegistry",
)
def empty_container_registry(ctx: ScanContext) -> Iterator[Finding]:
    for registry in ctx.list(RESOURCE_TYPE):
        registry_id = registry.get("id") or ""
        if not registry_id:
            continue
        stored = _stored_bytes(ctx, registry_id)
        # None means the registry would not say. Reporting it as empty on the
        # strength of a failed call would propose deleting a registry that may
        # be full.
        if stored == 0:
            yield build_finding(ctx, registry)
