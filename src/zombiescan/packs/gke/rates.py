"""GKE's own rate specs.

Core knows nothing about a cluster management fee, and should not have to.
The pack registers the section it needs here, at import time, and prices its
findings through ``ctx.pricing.rate`` like any other check.
"""

from __future__ import annotations

from zombiescan.pricing.rates import RateSpec, register_rate

# Google charges the same $0.10/hour management fee in every region, and
# publishes it against the region "global", so there is no region layer.
register_rate(
    RateSpec(
        "gke.cluster_month",
        "gke_cluster_hour",
        per_hour=True,
        scope="global",
        pack="gke",
    )
)
