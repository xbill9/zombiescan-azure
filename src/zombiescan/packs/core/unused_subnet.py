"""Subnets with no addresses in use.

A subnet costs nothing. It is reported because it holds an IP range, and a
range held by a subnet nothing uses is a range that cannot be reused, which is
how a VPC runs out of address space with nothing running in it. Peering a VPC
fails the same way: overlapping ranges are rejected whether or not anything
occupies them.

Only subnets in networks running no instances are reported, so a spare subnet
inside a live VPC -- kept for a workload that has not launched yet -- is left
alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-subnet"

# Subnets Google creates for its own plumbing. Deleting one breaks the load
# balancer or the private connection it serves, so they are never reported.
RESERVED_PURPOSES = frozenset(
    {
        "INTERNAL_HTTPS_LOAD_BALANCER",
        "REGIONAL_MANAGED_PROXY",
        "GLOBAL_MANAGED_PROXY",
        "PRIVATE_SERVICE_CONNECT",
        "PEER_MIGRATION",
    }
)


def build_finding(ctx: ScanContext, location: str, subnet: dict[str, Any]) -> Finding:
    name = subnet["name"]
    network = gcp.last_segment(subnet.get("network"))
    ranges = [subnet.get("ipCidrRange")] + [
        secondary.get("ipCidrRange") for secondary in subnet.get("secondaryIpRanges") or []
    ]
    held = [r for r in ranges if r]

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="subnet",
        project=ctx.project,
        location=location,
        reason=(
            f"Subnet holds {', '.join(held)} in network '{network}', which runs no "
            f"instances, so the range is reserved against nothing"
        ),
        monthly_cost=0.0,
        remediation=helpers.gcloud(
            f"gcloud compute networks subnets delete {helpers.arg(name)}", ctx.project, location
        ),
        details={
            "network": network,
            "ip_cidr_range": subnet.get("ipCidrRange"),
            "secondary_ranges": [
                {"name": s.get("rangeName"), "range": s.get("ipCidrRange")}
                for s in subnet.get("secondaryIpRanges") or []
            ],
            "purpose": subnet.get("purpose"),
            "private_ip_google_access": bool(subnet.get("privateIpGoogleAccess")),
            "note": "subnets are free; this is reported because the IP range cannot be reused",
        },
    )


@check(CHECK_NAME, "Subnets reserving ranges nothing uses", apis="compute")
def unused_subnet(ctx: ScanContext) -> Iterator[Finding]:
    populated = helpers.instances_by_network(ctx)
    for scope, subnet in gcp.aggregated(
        ctx.client("compute"), "subnetworks", "subnetworks", project=ctx.project
    ):
        if subnet.get("purpose") in RESERVED_PURPOSES:
            continue
        if populated.get(gcp.last_segment(subnet.get("network")), 0):
            continue
        yield build_finding(ctx, gcp.location_from_scope(scope), subnet)
