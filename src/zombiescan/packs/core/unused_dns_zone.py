"""Public DNS zones holding no records.

Azure charges per hosted zone per month -- $0.50 for the first 25 in a
subscription, $0.10 for the ones after that -- plus a charge per million
queries. A zone created for a domain that was never delegated, or for one
that moved elsewhere, keeps paying the hosting charge and answers nothing.

A zone is reported when the only record sets in it are the ones Azure created:
the SOA and the NS set at the apex. Every zone has those and they are not
billed as record sets, so a zone with exactly two is a zone nobody has put
anything in.

The saving is the tier the subscription is actually in, not the headline
first-tier price -- a subscription with thirty zones saves $0.10 by deleting
one, not $0.50.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-dns-zone"

RESOURCE_TYPE = "Microsoft.Network/dnszones"

# The SOA and the apex NS set, which Azure creates with every zone and
# charges nothing for.
BUILT_IN_RECORD_SETS = 2


def build_finding(ctx: ScanContext, zone: dict[str, Any], zone_count: int) -> Finding:
    name = zone["name"]
    arm_id = zone.get("id") or ""
    group = zone.get("resourceGroup") or azure.resource_group_of(arm_id)
    properties = helpers.properties(zone)

    price, approximate = ctx.pricing.rate("dns.zone_month", zone_count=zone_count)
    records = int(properties.get("numberOfRecordSets") or 0)
    tier = "first 25" if zone_count <= 25 else "beyond the first 25"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="dns-zone",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        # A public DNS zone has no region: Azure hosts it on an anycast name
        # server estate and prices it against a billing geography.
        location=azure.GLOBAL,
        reason=(
            f"DNS zone holds only the SOA and NS records Azure created with it, and is "
            f"billed as one of this subscription's {zone_count} zone(s)"
        ),
        monthly_cost=price,
        remediation=helpers.az(
            f"az network dns zone delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate,
        details={
            "record_sets": records,
            "name_servers": properties.get("nameServers") or [],
            "zones_in_subscription": zone_count,
            "priced_at_tier": tier,
            "tags": zone.get("tags") or {},
            "note": (
                "hosting charge only; DNS queries are billed separately and a zone "
                "nobody has delegated receives none"
            ),
        },
    )


@check(CHECK_NAME, "DNS zones with no records", providers="Microsoft.Network")
def unused_dns_zone(ctx: ScanContext) -> Iterator[Finding]:
    zones = list(ctx.list(RESOURCE_TYPE))
    # The tier depends on how many zones the subscription has in total, not on
    # how many are empty, so the count comes from the whole list.
    zone_count = len(zones)
    for zone in zones:
        if int(helpers.properties(zone).get("numberOfRecordSets") or 0) > BUILT_IN_RECORD_SETS:
            continue
        yield build_finding(ctx, zone, zone_count)
