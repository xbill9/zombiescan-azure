"""Cloud SQL instances that are stopped but still paying for storage.

Stopping a Cloud SQL instance stops the vCPU and memory charge. Storage keeps
billing in full, and so do automated backups, so a stopped instance is not
free -- it is a database you cannot query that costs the same to store as one
you can.

An instance is stopped when its ``activationPolicy`` is NEVER, or when Google
reports its state as STOPPED. Both are checked: the policy is what the API
sets when you run ``gcloud sql instances patch --activation-policy=NEVER``,
and the state is what it reports afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "stopped-sql-instance"

STOPPED_STATES = frozenset({"STOPPED", "SUSPENDED"})
NEVER = "NEVER"

# Cloud SQL reports its storage as PD_SSD or PD_HDD; the price table keys the
# same two rates as ssd and hdd.
_STORAGE_VARIANTS = {"PD_SSD": "ssd", "PD_HDD": "hdd"}


def _is_stopped(instance: dict[str, Any]) -> bool:
    settings = instance.get("settings") or {}
    return instance.get("state") in STOPPED_STATES or settings.get("activationPolicy") == NEVER


def build_finding(ctx: ScanContext, instance: dict[str, Any]) -> Finding:
    name = instance["name"]
    settings = instance.get("settings") or {}
    location = instance.get("region") or gcp.GLOBAL
    size_gb = helpers.gb(settings.get("dataDiskSizeGb"))
    variant = _STORAGE_VARIANTS.get(settings.get("dataDiskType", ""), "ssd")
    price, approximate = ctx.pricing.rate(
        "sql.storage_gb_month", region=gcp.region_of(location), variant=variant
    )
    high_availability = settings.get("availabilityType") == "REGIONAL"
    # A regional (highly available) instance keeps a standby replica, and its
    # storage is billed too.
    multiplier = 2 if high_availability else 1

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="sql-instance",
        project=ctx.project,
        location=location,
        reason=(
            f"Cloud SQL instance is stopped, but its {size_gb:g} GB of "
            f"{variant.upper()} storage still bills"
            + (
                " twice over, because it is configured for high availability"
                if high_availability
                else ""
            )
        ),
        monthly_cost=size_gb * price * multiplier,
        remediation=(
            f"gcloud sql instances delete {helpers.arg(name)} --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "state": instance.get("state"),
            "activation_policy": settings.get("activationPolicy"),
            "database_version": instance.get("databaseVersion"),
            "tier": settings.get("tier"),
            "storage_gb": size_gb,
            "storage_type": settings.get("dataDiskType"),
            "high_availability": high_availability,
            "usd_per_gb_month": price,
            "note": (
                "storage only; vCPU and memory stop when the instance does, and "
                "automated backup storage is billed separately"
            ),
        },
    )


@check(CHECK_NAME, "Stopped Cloud SQL instances still paying for storage", apis="sqladmin")
def stopped_sql_instance(ctx: ScanContext) -> Iterator[Finding]:
    # Cloud SQL lists every instance in the project in one call, whatever
    # region it sits in, so there is no location loop here.
    for instance in gcp.paginate(
        ctx.client("sqladmin"), "instances", key="items", project=ctx.project
    ):
        if _is_stopped(instance):
            yield build_finding(ctx, instance)
