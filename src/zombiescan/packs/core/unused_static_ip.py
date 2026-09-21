"""Reserved static IP addresses attached to nothing.

Google bills a reserved external address whether or not anything is using it,
and bills an *idle* one at a higher hourly rate than one attached to a running
VM. An address left behind by a deleted instance is the purest form of this
waste: it does nothing and costs more than one doing work.

Both regional and global addresses are checked. A global address backs an
external HTTP(S) load balancer, and is the one people forget.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-static-ip"

# Google reports an address that nothing references as RESERVED. IN_USE is the
# only other state a healthy address is in.
UNUSED_STATUS = "RESERVED"


def build_finding(ctx: ScanContext, location: str, address: dict[str, Any]) -> Finding:
    name = address["name"]
    price, approximate = ctx.pricing.rate("static_ip.month", region=gcp.region_of(location))
    age = helpers.age_days(address.get("creationTimestamp"))
    is_global = location == gcp.GLOBAL

    reason = (
        f"{'Global' if is_global else 'Regional'} static IP {address.get('address', '')} "
        f"is reserved but attached to nothing"
    )
    if age is not None:
        reason += f"; reserved {age} days ago"

    scope_flag = "--global" if is_global else f"--region={location}"
    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="compute-address",
        project=ctx.project,
        location=location,
        reason=reason,
        monthly_cost=price,
        remediation=(
            f"gcloud compute addresses delete {helpers.arg(name)} {scope_flag} "
            f"--project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "address": address.get("address"),
            "address_type": address.get("addressType"),
            "ip_version": address.get("ipVersion", "IPV4"),
            "network_tier": address.get("networkTier"),
            "purpose": address.get("purpose"),
            "age_days": age,
            "description": address.get("description"),
        },
    )


@check(CHECK_NAME, "Unused static IP addresses", apis="compute")
def unused_static_ip(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("compute")
    for scope, address in gcp.aggregated(client, "addresses", "addresses", project=ctx.project):
        if address.get("status") == UNUSED_STATUS:
            yield build_finding(ctx, gcp.location_from_scope(scope), address)

    # Global addresses live in their own collection: aggregatedList does not
    # reach them, so an unused load balancer IP would go unreported.
    for address in gcp.paginate(client, "globalAddresses", project=ctx.project):
        if address.get("status") == UNUSED_STATUS:
            yield build_finding(ctx, gcp.GLOBAL, address)
