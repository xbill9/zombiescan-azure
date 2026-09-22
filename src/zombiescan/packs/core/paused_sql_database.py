"""SQL databases that are paused but still paying for storage.

A serverless SQL database auto-pauses after its idle delay and stops billing
compute. Its storage bills in full the whole time, and so do its backups, so a
database that paused eight months ago and was never resumed is a bill nobody
is watching and nothing is reading.

The finding is priced at the storage the database would release. Backup
storage is deliberately not included: point-in-time backups are retained on
their own schedule whether the database is paused or not, so they are not
freed by acting on this.

``master`` is skipped. Every SQL server has one, it is not billed, and
reporting one per server would bury the databases that matter.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "paused-sql-database"

RESOURCE_TYPE = "Microsoft.Sql/servers/databases"

PAUSED = "Paused"
SYSTEM_DATABASE = "master"

# ARM's service-tier names, mapped to the price table's storage variants.
TIER_VARIANTS = {
    "GeneralPurpose": "general_purpose",
    "BusinessCritical": "business_critical",
    "Hyperscale": "hyperscale",
}


def build_finding(ctx: ScanContext, database: dict[str, Any], server: str) -> Finding:
    name = database["name"]
    arm_id = database.get("id") or ""
    group = database.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(database)
    properties = helpers.properties(database)

    max_size_gb = helpers.bytes_to_gb(properties.get("maxSizeBytes"))
    tier = (database.get("sku") or {}).get("tier") or properties.get("currentServiceObjectiveName")
    variant = TIER_VARIANTS.get(str(tier), "general_purpose")
    price, approximate = ctx.pricing.rate(
        "sql.storage_gb_month", region=azure.region_of(location), variant=variant
    )
    paused_since = helpers.age_days(properties.get("pausedDate"))

    reason = (
        f"Database on {server} is paused; its {max_size_gb:g} GiB of provisioned "
        f"storage bills in full while its compute does not"
    )
    if paused_since is not None:
        reason += f"; paused {paused_since} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{server}/{name}",
        resource_type="sql-database",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=max_size_gb * price,
        remediation=helpers.az(
            f"az sql db delete --name {helpers.arg(name)} --server {helpers.arg(server)}",
            ctx.subscription,
            group,
        ),
        approximate_cost=approximate,
        details={
            "server": server,
            "database": name,
            "status": properties.get("status"),
            "service_tier": tier,
            "max_size_gb": max_size_gb,
            "paused_days_ago": paused_since,
            "auto_pause_delay_minutes": properties.get("autoPauseDelay"),
            "zone_redundant": properties.get("zoneRedundant"),
            "usd_per_gb_month": price,
            "note": (
                "provisioned storage only. Backup storage is billed separately and is "
                "retained on its own schedule, so it is not freed by deleting this"
            ),
        },
    )


@check(CHECK_NAME, "Paused SQL databases still paying for storage", providers="Microsoft.Sql")
def paused_sql_database(ctx: ScanContext) -> Iterator[Finding]:
    # Databases are listed per server, and a server's list call is the only
    # one ARM offers -- there is no subscription-wide database list. The
    # servers come back in one call, so this is one call plus one per server.
    servers = list(ctx.list("Microsoft.Sql/servers"))
    for server in servers:
        server_id = server.get("id") or ""
        if not server_id:
            continue
        for database in ctx.arm.list(f"{server_id}/databases", RESOURCE_TYPE):
            if database.get("name") == SYSTEM_DATABASE:
                continue
            if helpers.properties(database).get("status") != PAUSED:
                continue
            yield build_finding(ctx, database, server["name"])
