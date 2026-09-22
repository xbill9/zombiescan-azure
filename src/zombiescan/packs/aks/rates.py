"""AKS's own rate specs.

Core knows nothing about a cluster management fee, and should not have to.
The pack registers the section it needs here, at import time, and prices its
findings through ``ctx.pricing.rate`` like any other check.
"""

from __future__ import annotations

from zombiescan.pricing.rates import RateSpec, register_rate

# Keyed by the cluster's SKU tier as ARM spells it -- Free, Standard or
# Premium -- because the three are charged differently and a cluster says
# which it is. Free is stored as an explicit 0.0 rather than left missing, so
# a free cluster is priced at nothing *exactly* rather than marked as a rate
# the table does not know.
register_rate(
    RateSpec(
        "aks.cluster_month",
        "aks_cluster_hour",
        per_hour=True,
        variants=True,
        pack="aks",
    )
)
