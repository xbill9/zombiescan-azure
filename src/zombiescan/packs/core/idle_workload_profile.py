"""Dedicated Container Apps workload profiles no app is assigned to.

A Dedicated workload profile keeps ``minimumCount`` instances running and
bills their vCPU and memory by the hour whether or not an app is placed on
them. An environment whose apps all moved to Consumption, or to another
profile, keeps paying for the one they left: a D4 profile held at one instance
is about $225 a month for nothing. When it is the environment's only
Dedicated profile, removing it ends the Dedicated management fee too, and the
finding includes that.

A profile with ``minimumCount`` of zero scales to nothing and is not
reported. An environment with no app at all is
``empty-container-apps-environment``'s finding instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.packs.core.empty_container_apps_environment import (
    APP_TYPE,
    RESOURCE_TYPE,
    dedicated_profiles,
    management_cost,
    profile_cost,
    profiles_in_use,
)
from zombiescan.registry import check

CHECK_NAME = "idle-workload-profile"


def build_finding(
    ctx: ScanContext, environment: dict[str, Any], profile: dict[str, Any], only_dedicated: bool
) -> Finding:
    name = environment["name"]
    arm_id = environment.get("id") or ""
    group = environment.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(environment)
    region = azure.region_of(location)
    profile_name = str(profile.get("name") or "")
    kind = profile.get("workloadProfileType")
    instances = int(profile.get("minimumCount") or 0)

    cost, approximate = profile_cost(ctx, region, profile)
    if only_dedicated:
        fee, rough = management_cost(ctx, region)
        cost, approximate = cost + fee, approximate or rough

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{name}/{profile_name}",
        resource_type="workload-profile",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"Dedicated {kind} profile keeps {instances} instance(s) running with no app "
            "assigned to it"
        ),
        monthly_cost=cost,
        remediation=helpers.az(
            f"az containerapp env workload-profile delete --name {helpers.arg(name)} "
            f"--workload-profile-name {helpers.arg(profile_name)}",
            ctx.subscription,
            group,
        ),
        approximate_cost=approximate,
        details={
            "environment": name,
            "workload_profile": profile_name,
            "profile_type": kind,
            "minimum_count": instances,
            "maximum_count": profile.get("maximumCount"),
            "includes_management_fee": only_dedicated,
            "tags": environment.get("tags") or {},
            "note": "the profile's standing instances"
            + (", plus the environment's Dedicated management fee" if only_dedicated else ""),
        },
    )


@check(
    CHECK_NAME,
    "Dedicated workload profiles running no app",
    providers="Microsoft.App",
    uncleanable=(
        "removing a profile rewrites the environment's workload profile list, and the "
        "merge behaviour of that update is not verified; run the printed "
        "'az containerapp env workload-profile delete' instead"
    ),
)
def idle_workload_profile(ctx: ScanContext) -> Iterator[Finding]:
    in_use = profiles_in_use(ctx.list(APP_TYPE))
    for environment in ctx.list(RESOURCE_TYPE):
        used = in_use.get(str(environment.get("id") or "").lower())
        if used is None:
            continue
        dedicated = dedicated_profiles(environment)
        for profile in dedicated:
            idle = str(profile.get("name") or "").lower() not in used
            if idle and int(profile.get("minimumCount") or 0) > 0:
                yield build_finding(ctx, environment, profile, only_dedicated=len(dedicated) == 1)
