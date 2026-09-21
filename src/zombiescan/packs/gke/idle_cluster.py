"""GKE clusters with no nodes behind them.

A GKE cluster bills a flat management fee per hour -- the same fee whether it
runs a thousand pods or none. Scaling every node pool to zero removes the node
cost and leaves that fee exactly where it was, which is why a cluster left
"scaled down" is still one of the more expensive things in a project.

Google gives one zonal cluster per billing account free, so the cheapest
cluster in an account may cost nothing. That allowance is per billing account
and a project scan cannot see which cluster it applies to, so the finding says
so rather than quietly assuming it does not apply.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "gke-idle-cluster"


def _node_count(cluster: dict[str, Any]) -> int:
    """Nodes across every pool, from the pools rather than the cluster total.

    ``currentNodeCount`` is absent on a cluster that has scaled to zero, and
    absent reads as "unknown" rather than as zero, so the pools are summed.
    """
    return sum(int(pool.get("initialNodeCount") or 0) for pool in cluster.get("nodePools") or [])


def build_finding(ctx: ScanContext, cluster: dict[str, Any]) -> Finding:
    name = cluster["name"]
    location = cluster.get("location", gcp.GLOBAL)
    price, approximate = ctx.pricing.rate("gke.cluster_month")
    pools = cluster.get("nodePools") or []
    autopilot = bool((cluster.get("autopilot") or {}).get("enabled"))
    age = helpers.age_days(cluster.get("createTime"))

    reason = (
        f"GKE cluster runs no nodes across its {len(pools)} node pool(s), but the cluster "
        f"management fee is charged per hour regardless"
    )
    if age is not None:
        reason += f"; created {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="gke-cluster",
        project=ctx.project,
        location=location,
        reason=reason,
        monthly_cost=price,
        remediation=(
            f"gcloud container clusters delete {helpers.arg(name)} --location={location} "
            f"--project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "status": cluster.get("status"),
            "autopilot": autopilot,
            "node_pools": [
                {
                    "name": pool.get("name"),
                    "nodes": int(pool.get("initialNodeCount") or 0),
                    "machine_type": (pool.get("config") or {}).get("machineType"),
                }
                for pool in pools
            ],
            "kubernetes_version": cluster.get("currentMasterVersion"),
            "age_days": age,
            "note": (
                "Google gives one zonal cluster per billing account free. That allowance "
                "is per billing account, which a project scan cannot see, so this may "
                "be the free one"
            ),
        },
    )


@check(CHECK_NAME, "GKE clusters running no nodes", apis="container")
def gke_idle_cluster(ctx: ScanContext) -> Iterator[Finding]:
    # locations/- covers zonal and regional clusters in one call.
    for cluster in gcp.paginate(
        ctx.client("container"),
        "projects.locations.clusters",
        key="clusters",
        parent=ctx.any_location(),
    ):
        if _node_count(cluster) == 0:
            yield build_finding(ctx, cluster)
