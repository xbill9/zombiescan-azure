"""Container Apps environments with no container app in them.

A Consumption-only environment costs nothing empty and is hygiene: it still
holds a virtual network, a Log Analytics connection and a static IP that read
as something in use. One with a Dedicated workload profile is not free at
all -- a Dedicated profile bills its instances' vCPU and memory by the hour
from ``minimumCount`` upward, and the environment pays a management fee while
it has any Dedicated profile, whatever runs there.

Instance sizes follow the profile type, per Microsoft's workload profile
table: D is 4 GiB per vCPU, E is 8, DC is 4, each with the vCPU count in its
name. GPU profiles are billed differently and are reported unpriced.

A Dedicated profile with no app in an environment that does have apps is
``idle-workload-profile``'s finding; the helpers for pricing one live here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "empty-container-apps-environment"

RESOURCE_TYPE = "Microsoft.App/managedEnvironments"
APP_TYPE = "Microsoft.App/containerApps"

# GiB of memory per vCPU for each Dedicated family: D4 is 4 vCPU and 16 GiB,
# E4 is 4 vCPU and 32 GiB, DC4 is 4 vCPU and 16 GiB.
GIB_PER_VCPU = {"D": 4, "E": 8, "DC": 4}


def is_dedicated(profile: dict[str, Any]) -> bool:
    return not str(profile.get("workloadProfileType") or "").startswith(("Consumption", "Flex"))


def dedicated_profiles(environment: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = helpers.properties(environment).get("workloadProfiles") or []
    return [profile for profile in profiles if is_dedicated(profile)]


def instance_size(profile_type: str) -> tuple[int, int] | None:
    """``(vCPU, GiB)`` of one instance of a Dedicated profile type, or None."""
    match = re.fullmatch(r"(DC|D|E)(\d+)", profile_type or "")
    if not match:
        return None
    vcpu = int(match.group(2))
    return vcpu, vcpu * GIB_PER_VCPU[match.group(1)]


def profile_cost(ctx: ScanContext, region: str, profile: dict[str, Any]) -> tuple[float, bool]:
    """What a Dedicated profile's standing instances cost a month."""
    instances = int(profile.get("minimumCount") or 0)
    size = instance_size(str(profile.get("workloadProfileType") or ""))
    if size is None:
        return 0.0, bool(instances)
    vcpu_rate, a = ctx.pricing.rate("container_apps.month", region=region, variant="dedicated_vcpu")
    gib_rate, b = ctx.pricing.rate("container_apps.month", region=region, variant="dedicated_gib")
    return instances * (size[0] * vcpu_rate + size[1] * gib_rate), a or b


def management_cost(ctx: ScanContext, region: str) -> tuple[float, bool]:
    """The Dedicated plan's per-environment fee, a month."""
    return ctx.pricing.rate("container_apps.month", region=region, variant="dedicated_management")


def environment_of(app: dict[str, Any]) -> str:
    properties = helpers.properties(app)
    return str(properties.get("environmentId") or properties.get("managedEnvironmentId") or "")


def profiles_in_use(apps: Iterator[dict[str, Any]]) -> dict[str, set[str]]:
    """The workload profiles apps run on, keyed by lowercased environment id."""
    in_use: dict[str, set[str]] = {}
    for app in apps:
        profile = str(helpers.properties(app).get("workloadProfileName") or "").lower()
        in_use.setdefault(environment_of(app).lower(), set()).add(profile)
    return in_use


def build_finding(ctx: ScanContext, environment: dict[str, Any]) -> Finding:
    name = environment["name"]
    arm_id = environment.get("id") or ""
    group = environment.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(environment)
    region = azure.region_of(location)
    dedicated = dedicated_profiles(environment)

    cost, approximate = 0.0, False
    for profile in dedicated:
        amount, rough = profile_cost(ctx, region, profile)
        cost, approximate = cost + amount, approximate or rough
    if dedicated:
        fee, rough = management_cost(ctx, region)
        cost, approximate = cost + fee, approximate or rough

    reason = "Container Apps environment holds no container app"
    if dedicated:
        reason += f"; its {len(dedicated)} Dedicated profile(s) and management fee bill regardless"
    else:
        reason += "; Consumption-only, so it costs nothing but still holds its network and IP"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="container-apps-environment",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=helpers.az(
            f"az containerapp env delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate,
        details={
            "dedicated_profiles": [
                {
                    "name": p.get("name"),
                    "type": p.get("workloadProfileType"),
                    "minimum_count": p.get("minimumCount"),
                }
                for p in dedicated
            ],
            "static_ip": helpers.properties(environment).get("staticIp"),
            "tags": environment.get("tags") or {},
            "note": (
                "Dedicated instances at minimumCount plus the management fee; a "
                "Consumption-only environment costs nothing empty. Deleting it releases "
                "its static IP"
            ),
        },
    )


@check(CHECK_NAME, "Container Apps environments with no app", providers="Microsoft.App")
def empty_container_apps_environment(ctx: ScanContext) -> Iterator[Finding]:
    in_use = profiles_in_use(ctx.list(APP_TYPE))
    for environment in ctx.list(RESOURCE_TYPE):
        if str(environment.get("id") or "").lower() not in in_use:
            yield build_finding(ctx, environment)
