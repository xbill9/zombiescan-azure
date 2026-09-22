"""Where AKS's prices come from.

One fetcher against the Azure Kubernetes Service meters. It is registered
here rather than in core's refresher for the same reason the rate spec is:
core does not know what a cluster management fee is, and a pack that could not
refresh its own rates would go stale the first time Microsoft changed them.
"""

from __future__ import annotations

from typing import Any

from zombiescan.pricing.refresh import RefreshContext, by_region, price_fetcher

# The meter that charges for each SKU tier. The Free tier has no meter at all,
# which is the whole point of the finding: there is nothing to publish because
# there is nothing to charge.
TIER_METERS = {
    "Standard Uptime SLA": "Standard",
    "Standard Long Term Support": "Premium",
}


@price_fetcher("aks_cluster_hour", label="AKS cluster management fee", pack="aks")
def fetch_cluster_fee(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {tier: usd per hour}}`` of cluster uptime, whatever runs on it.

    The fee is charged per cluster regardless of how many nodes it has, which
    is what makes an empty Standard-tier cluster expensive -- and what makes
    an empty Free-tier one cost nothing.
    """
    rows = ctx.rows("serviceName eq 'Azure Kubernetes Service'", "unitOfMeasure eq '1 Hour'")
    table = by_region(rows, lambda row: TIER_METERS.get(str(row.get("meterName") or "")))
    # Free is a real rate, not a missing one.
    for tiers in table.values():
        tiers["Free"] = 0.0
    return {"aks_cluster_hour": table}
