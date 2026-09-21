"""Filestore instances nothing can mount.

Filestore bills provisioned capacity by the hour from the moment the instance
is created, and the minimum instance is large -- a 1 TiB zonal share is the
smallest Basic HDD tier Google sells. An instance in a VPC that runs no
compute has nothing that could mount it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-filestore"

# Filestore's API tier names map onto the price table's capacity rates.
_TIERS = {
    "STANDARD": "basic-hdd",
    "BASIC_HDD": "basic-hdd",
    "PREMIUM": "basic-ssd",
    "BASIC_SSD": "basic-ssd",
    "ZONAL": "zonal",
    "REGIONAL": "regional",
    "ENTERPRISE": "enterprise",
}


def build_finding(ctx: ScanContext, instance: dict[str, Any], network: str) -> Finding:
    # Filestore names an instance by its full path; the location sits between
    # "locations" and "instances" in it.
    full_name = instance["name"]
    parts = full_name.split("/")
    location = parts[parts.index("locations") + 1] if "locations" in parts else gcp.GLOBAL
    name = gcp.last_segment(full_name)

    shares = instance.get("fileShares") or []
    capacity_gb = sum(helpers.gb(share.get("capacityGb")) for share in shares)
    tier = instance.get("tier", "STANDARD")
    price, approximate = ctx.pricing.rate(
        "filestore.gb_month", region=gcp.region_of(location), variant=_TIERS.get(tier, "zonal")
    )
    age = helpers.age_days(instance.get("createTime"))

    reason = (
        f"{capacity_gb:g} GB {tier} Filestore instance on network '{network}', "
        f"which runs no instances that could mount it"
    )
    if age is not None:
        reason += f"; created {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="filestore-instance",
        project=ctx.project,
        location=location,
        reason=reason,
        monthly_cost=capacity_gb * price,
        remediation=(
            f"gcloud filestore instances delete {helpers.arg(name)} --location={location} "
            f"--project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "tier": tier,
            "capacity_gb": capacity_gb,
            "share_count": len(shares),
            "network": network,
            "state": instance.get("state"),
            "age_days": age,
            "usd_per_gb_month": price,
            "note": "deleting a Filestore instance deletes the file data in it",
        },
    )


@check(
    CHECK_NAME,
    "Filestore instances nothing can mount",
    apis=("file", "compute"),
    uncleanable=(
        "deleting a Filestore instance deletes the file data on it, and the backup that "
        "would make that safe has to be placed in a region and tier nothing in the API "
        "chooses for you. zombiescan prints the delete command and leaves the backup "
        "decision to you"
    ),
)
def idle_filestore(ctx: ScanContext) -> Iterator[Finding]:
    populated = helpers.instances_by_network(ctx)
    for instance in gcp.paginate(
        ctx.client("file"),
        "projects.locations.instances",
        key="instances",
        parent=ctx.any_location(),
    ):
        networks = instance.get("networks") or []
        names = [n.get("network", "") for n in networks]
        if any(populated.get(name, 0) for name in names):
            continue
        yield build_finding(ctx, instance, ", ".join(n for n in names if n) or "unknown")
