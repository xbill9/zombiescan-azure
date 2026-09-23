"""Azure Machine Learning and Foundry compute left running.

Two shapes, both billed at the VM rate of their size for every hour a node is
up:

* **A compute instance running with no idle shutdown.** A notebook VM started
  for an afternoon keeps billing until someone stops it, and one with no
  ``idleTimeBeforeShutdown`` and no schedule never stops on its own.
* **A compute cluster holding idle nodes because ``minNodeCount`` is above
  zero.** Nodes above the minimum scale down after the idle timeout; the
  minimum stays up whether a job ever arrives. The finding counts the nodes
  that are idle right now, up to the minimum.

The price is the Linux compute rate for the VM size. A stopped compute
instance still bills its OS disk, which is not counted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-ml-compute"

WORKSPACE_TYPE = "Microsoft.MachineLearningServices/workspaces"
RESOURCE_TYPE = "Microsoft.MachineLearningServices/workspaces/computes"

INSTANCE = "ComputeInstance"
CLUSTER = "AmlCompute"


def _inner(compute: dict[str, Any]) -> dict[str, Any]:
    """The compute-specific block: ARM nests it as ``properties.properties``."""
    return helpers.properties(compute).get("properties") or {}


def never_stops(compute: dict[str, Any]) -> bool:
    """A running compute instance with no idle shutdown and no schedule."""
    inner = _inner(compute)
    schedules = (inner.get("schedules") or {}).get("computeStartStop") or []
    stops = [s for s in schedules if str(s.get("action") or "").lower() == "stop"]
    return (
        str(inner.get("state") or "") == "Running"
        and not inner.get("idleTimeBeforeShutdown")
        and not stops
    )


def idle_minimum(compute: dict[str, Any]) -> int:
    """How many of a cluster's idle nodes its ``minNodeCount`` is holding up."""
    inner = _inner(compute)
    minimum = int((inner.get("scaleSettings") or {}).get("minNodeCount") or 0)
    idle = int((inner.get("nodeStateCounts") or {}).get("idleNodeCount") or 0)
    return min(minimum, idle)


def scale_to_zero(scale: dict[str, Any]) -> dict[str, Any]:
    """The update body that drops a cluster's minimum to zero and keeps the rest.

    The shape is ARM's ``ClusterUpdateParameters``: scale settings nest under
    ``properties.properties``, and ``maxNodeCount`` is required.
    """
    settings = {"minNodeCount": 0, "maxNodeCount": int(scale.get("maxNodeCount") or 1)}
    if scale.get("nodeIdleTimeBeforeScaleDown"):
        settings["nodeIdleTimeBeforeScaleDown"] = scale["nodeIdleTimeBeforeScaleDown"]
    return {"properties": {"properties": {"scaleSettings": settings}}}


def build_finding(ctx: ScanContext, compute: dict[str, Any], nodes: int) -> Finding:
    name = compute["name"]
    arm_id = compute.get("id") or ""
    workspace = azure.name_of(arm_id.rsplit("/computes/", 1)[0])
    group = compute.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(compute)
    kind = helpers.properties(compute).get("computeType")
    inner = _inner(compute)
    size = str(inner.get("vmSize") or "")

    rate, approximate = ctx.pricing.rate("vm.month", region=azure.region_of(location), variant=size)
    cost = rate * nodes

    if kind == INSTANCE:
        reason = f"{size} compute instance is running with no idle shutdown or stop schedule"
        remediation = f"az resource invoke-action --action stop --ids {helpers.arg(arm_id)}"
    else:
        scale = inner.get("scaleSettings") or {}
        reason = (
            f"{size} cluster keeps {nodes} idle node(s) up because minNodeCount is "
            f"{scale.get('minNodeCount')}"
        )
        body = json.dumps(scale_to_zero(scale), separators=(",", ":"))
        url = f"https://management.azure.com{arm_id}?api-version={azure.api_version(RESOURCE_TYPE)}"
        remediation = f"az rest --method patch --url {helpers.arg(url)} --body {helpers.arg(body)}"

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{workspace}/{name}",
        resource_type="ml-compute",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        # Both commands address the compute by its id or URL, so there is no
        # resource group to pass.
        remediation=helpers.az(remediation, ctx.subscription),
        approximate_cost=approximate,
        details={
            "workspace": workspace,
            "compute_type": kind,
            "vm_size": size,
            "billed_nodes": nodes,
            "state": inner.get("state"),
            "scale_settings": inner.get("scaleSettings"),
            "tags": compute.get("tags") or {},
            "note": "Linux compute rate per node; OS disks and any licence are not included",
        },
    )


@check(CHECK_NAME, "ML compute running or held idle", providers="Microsoft.MachineLearningServices")
def idle_ml_compute(ctx: ScanContext) -> Iterator[Finding]:
    # Computes are listed per workspace. A workspace that refuses the call
    # raises rather than being skipped: a running GPU instance is the most
    # expensive thing this check can find.
    for workspace in ctx.list(WORKSPACE_TYPE):
        workspace_id = workspace.get("id")
        if not workspace_id:
            continue
        for compute in ctx.arm.list(f"{workspace_id}/computes", RESOURCE_TYPE):
            kind = helpers.properties(compute).get("computeType")
            if kind == INSTANCE and never_stops(compute):
                yield build_finding(ctx, compute, 1)
            elif kind == CLUSTER and (nodes := idle_minimum(compute)):
                yield build_finding(ctx, compute, nodes)
