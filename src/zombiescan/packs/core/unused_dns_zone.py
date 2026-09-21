"""Cloud DNS managed zones publishing nothing.

Every managed zone bills monthly whether or not anything resolves against it.
A zone created for a project that was never finished holds only the two record
sets Cloud DNS creates with it -- the SOA and the NS set -- and neither can be
deleted, so a zone reporting exactly those two is publishing nothing and no
second judgement call is needed.

The finding is priced at the **marginal** rate. Zones cost $0.20/month for the
first 25 in a project and less beyond, so removing one from a project with
thirty saves the second tier's rate, not the headline one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-dns-zone"

# The records Cloud DNS creates with every zone and refuses to delete.
UNDELETABLE_TYPES = frozenset({"SOA", "NS"})


def build_finding(
    ctx: ScanContext, zone: dict[str, Any], records: list[dict[str, Any]], zone_count: int
) -> Finding:
    name = zone["name"]
    price, approximate = ctx.pricing.rate("dns.zone_month", zone_count=zone_count)
    visibility = (zone.get("visibility") or "public").lower()
    age = helpers.age_days(zone.get("creationTime"))

    reason = (
        f"{visibility.capitalize()} zone '{zone.get('dnsName', '')}' holds only the SOA and "
        f"NS records Cloud DNS creates with a zone, so it publishes nothing"
    )
    if age is not None:
        reason += f"; created {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="dns-zone",
        project=ctx.project,
        location=gcp.GLOBAL,
        reason=reason,
        monthly_cost=price,
        remediation=(
            f"gcloud dns managed-zones delete {helpers.arg(name)} --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "dns_name": zone.get("dnsName"),
            "visibility": visibility,
            "record_set_count": len(records),
            "record_types": sorted({r.get("type", "") for r in records}),
            "age_days": age,
            "zones_in_project": zone_count,
            "note": (
                "priced at the marginal rate: what deleting one zone from this project "
                "actually saves, given how many zones it already has"
            ),
        },
    )


@check(CHECK_NAME, "Cloud DNS zones publishing nothing", apis="dns")
def unused_dns_zone(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("dns")
    zones = list(gcp.paginate(client, "managedZones", key="managedZones", project=ctx.project))
    for zone in zones:
        records = list(
            gcp.paginate(
                client,
                "resourceRecordSets",
                key="rrsets",
                project=ctx.project,
                managedZone=zone["name"],
            )
        )
        if any(record.get("type") not in UNDELETABLE_TYPES for record in records):
            continue
        yield build_finding(ctx, zone, records, len(zones))
