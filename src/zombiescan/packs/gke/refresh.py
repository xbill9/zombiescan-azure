"""Where GKE's prices come from.

One fetcher against the Kubernetes Engine service. It is registered here
rather than in core's refresher for the same reason the rate spec is: core
does not know what a cluster management fee is, and a pack that could not
refresh its own rates would go stale the first time Google changed them.
"""

from __future__ import annotations

from typing import Any

from zombiescan.pricing.refresh import RefreshContext, price_fetcher, unit_price

# Google sells the same management fee under four descriptions -- zonal,
# regional, Autopilot and the extended-period tier -- at the same hourly rate.
# A standard zonal cluster is the one every finding here is priced as.
CLUSTER_SKU = "Zonal Kubernetes Clusters"


@price_fetcher("gke_cluster_hour", label="GKE cluster management fee", pack="gke")
def fetch_cluster_fee(ctx: RefreshContext) -> dict[str, Any]:
    """USD per hour of cluster uptime, charged per cluster regardless of size.

    Published against the region "global", so there is one rate. The fee is
    what makes an empty cluster expensive: it is charged whether the cluster
    runs a thousand pods or none.
    """
    for sku in ctx.skus("Kubernetes Engine"):
        if sku.get("description") == CLUSTER_SKU:
            price = unit_price(sku)
            if price is not None:
                return {"gke_cluster_hour": {"_value": price}}
    return {"gke_cluster_hour": {}}
