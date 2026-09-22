"""AKS clusters with no nodes behind them.

An AKS cluster's control plane is charged by SKU tier, and the tier is what
decides whether an idle cluster is expensive or free:

* **Free** -- no control plane charge at all. An empty Free-tier cluster
  costs nothing, and the finding says so rather than quoting a fee it does
  not pay.
* **Standard** -- $0.10 per hour, about $73 a month, charged whether the
  cluster runs a thousand pods or none.
* **Premium** -- $0.60 per hour for long-term support.

Scaling every node pool to zero removes the node cost and leaves the tier fee
exactly where it was, which is why a Standard cluster left "scaled down" is
still one of the more expensive things in a subscription. On the Free tier the
same action really does take the bill to zero, which is the reverse of the
advice that holds on Google Cloud.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "aks-idle-cluster"

DEFAULT_TIER = "Free"


def _node_count(cluster: dict[str, Any]) -> int:
    """Nodes across every pool, summed from the pools themselves.

    ``count`` is what the pool is scaled to right now; a cluster scaled to
    zero reports zero here and reports nothing useful at the cluster level.
    """
    properties = helpers.properties(cluster)
    return sum(int(pool.get("count") or 0) for pool in properties.get("agentPoolProfiles") or [])


def _tier(cluster: dict[str, Any]) -> str:
    return (cluster.get("sku") or {}).get("tier") or DEFAULT_TIER


def build_finding(ctx: ScanContext, cluster: dict[str, Any]) -> Finding:
    name = cluster["name"]
    arm_id = cluster.get("id") or ""
    group = cluster.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(cluster)
    properties = helpers.properties(cluster)
    tier = _tier(cluster)

    price, approximate = ctx.pricing.rate(
        "aks.cluster_month", region=azure.region_of(location), variant=tier
    )
    pools = properties.get("agentPoolProfiles") or []
    age = helpers.age_days(properties.get("createdAt") or cluster.get("createdAt"))

    if price:
        reason = (
            f"AKS cluster runs no nodes across its {len(pools)} node pool(s), but the "
            f"{tier} tier control plane is charged per hour regardless"
        )
    else:
        # Saying "$0.00/month wasted" without explaining why reads as a broken
        # price table rather than as the fact it is.
        reason = (
            f"AKS cluster runs no nodes across its {len(pools)} node pool(s). The "
            f"{tier} tier control plane is not charged, so this costs nothing today"
        )
    if age is not None:
        reason += f"; created {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="aks-cluster",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=price,
        remediation=helpers.az(
            f"az aks delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate and bool(price),
        details={
            "sku_tier": tier,
            "power_state": (properties.get("powerState") or {}).get("code"),
            "provisioning_state": properties.get("provisioningState"),
            "node_pools": [
                {
                    "name": pool.get("name"),
                    "nodes": int(pool.get("count") or 0),
                    "vm_size": pool.get("vmSize"),
                    "mode": pool.get("mode"),
                }
                for pool in pools
            ],
            "kubernetes_version": properties.get("currentKubernetesVersion")
            or properties.get("kubernetesVersion"),
            "age_days": age,
            "note": (
                "control plane only. The node pools are empty, so there is no VM cost "
                "to add; any disks they left behind are reported by unattached-disk"
            ),
        },
    )


@check(CHECK_NAME, "AKS clusters running no nodes", providers="Microsoft.ContainerService")
def aks_idle_cluster(ctx: ScanContext) -> Iterator[Finding]:
    # One list call covers every resource group and every region.
    for cluster in ctx.list("Microsoft.ContainerService/managedClusters"):
        if _node_count(cluster) == 0:
            yield build_finding(ctx, cluster)
