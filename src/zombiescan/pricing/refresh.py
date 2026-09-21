"""Regenerate ``table.json`` from the Cloud Billing Catalog API.

Run this, not your memory, when a price looks wrong:

    uv run python -m zombiescan.pricing.refresh

Reads ``cloudbilling.googleapis.com`` with Application Default Credentials.
Prices are on-demand USD list prices: they ignore committed use discounts,
sustained use discounts, private pricing and credits.

**How a SKU becomes a rate.** The catalog is not organised by product the way
the console is. A SKU carries a ``category`` (resource family, resource group,
usage type), a free-text ``description``, the regions it applies to, and a
tiered price. Nothing in it names "pd-balanced". So each fetcher below states
the exact family/group/description it matches, and a matcher that stops
matching yields an empty section -- which ``main`` refuses to write over a
populated one, rather than quietly pricing every disk at zero.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import google.auth
import googleapiclient.discovery

TABLE_PATH = pathlib.Path(__file__).with_name("table.json")

# The catalog prices a month of a per-month SKU directly, but hourly SKUs
# (NAT uptime, GKE clusters, forwarding rules) need a month's worth of hours.
# Google bills a 730-hour month.
HOURS_PER_MONTH = 730

FALLBACK_REGION = "us-central1"

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


@dataclass
class RefreshContext:
    """What a fetcher is handed: the catalog client and a service-id lookup.

    Service ids are opaque strings (Compute Engine is ``6F81-5844-456A``), so
    fetchers name a service by its display name and this resolves it. That
    keeps a hardcoded id out of every fetcher, and makes a service Google
    renames fail loudly at lookup rather than silently returning no SKUs.
    """

    billing: Any
    services: dict[str, str] = field(default_factory=dict)

    def service_id(self, display_name: str) -> str:
        try:
            return self.services[display_name]
        except KeyError:
            raise KeyError(
                f"no billing service named {display_name!r}. "
                "Google may have renamed it; check the /services listing."
            ) from None

    def skus(self, display_name: str) -> Iterator[dict[str, Any]]:
        """Every on-demand SKU of one service."""
        parent = f"services/{self.service_id(display_name)}"
        request = self.billing.services().skus().list(parent=parent, pageSize=5000)
        while request is not None:
            response = request.execute()
            for sku in response.get("skus", []):
                if sku.get("category", {}).get("usageType") == "OnDemand":
                    yield sku
            request = self.billing.services().skus().list_next(request, response)


@dataclass(frozen=True)
class Fetcher:
    """One registered source of price-table sections."""

    sections: tuple[str, ...]
    fn: Callable[[RefreshContext], dict[str, Any]]
    label: str
    pack: str = "core"


FETCHERS: list[Fetcher] = []


def price_fetcher(
    *sections: str, label: str, pack: str = "core"
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a fetcher for one or more table sections.

    The function returns ``{section: data}``. Declaring the sections up front
    lets ``main`` report what a pack contributed, and lets a refresh that skips
    a pack leave that pack's existing rates in the table untouched rather than
    dropping them.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        FETCHERS.append(Fetcher(tuple(sections), fn, label, pack))
        return fn

    return decorator


# --------------------------------------------------------------------------
# Reading one SKU
# --------------------------------------------------------------------------


def usd(sku: dict[str, Any], tier: int = 0) -> float | None:
    """The USD unit price of one pricing tier, or None if the SKU has none.

    Callers that want "the rate this resource is billed at" want
    ``unit_price``, not this: many SKUs open with a free allowance, and tier 0
    of those is zero. This is for reading a named tier deliberately, the way
    the Cloud DNS fetcher reads each zone tier in turn.
    """
    infos = sku.get("pricingInfo") or []
    if not infos:
        return None
    rates = infos[-1].get("pricingExpression", {}).get("tieredRates") or []
    if tier >= len(rates):
        return None
    amount = rates[tier].get("unitPrice") or {}
    return int(amount.get("units", 0)) + int(amount.get("nanos", 0)) / 1e9


def unit_price(sku: dict[str, Any]) -> float | None:
    """What this SKU actually charges per unit, skipping any free allowance.

    Google fronts a lot of SKUs with a giveaway priced at zero -- the first
    30 GB of standard Persistent Disk, the first 0.5 GB of Artifact Registry,
    the first six secret versions, the first 5 GB of Cloud Storage. Reading
    tier 0 of those records the rate as free, which does not fail loudly: it
    silently prices every finding in the section at nothing, and the scan
    reports a clean project.

    So the rate is the first tier that charges. A SKU that is genuinely free
    at every tier returns 0.0, and one with no tiers at all returns None.
    """
    infos = sku.get("pricingInfo") or []
    if not infos:
        return None
    rates = infos[-1].get("pricingExpression", {}).get("tieredRates") or []
    for index in range(len(rates)):
        price = usd(sku, tier=index)
        if price:
            return price
    return 0.0 if rates else None


def regions_of(sku: dict[str, Any]) -> list[str]:
    return [r for r in sku.get("serviceRegions", []) if r and r != "global"]


def is_global(sku: dict[str, Any]) -> bool:
    return "global" in (sku.get("serviceRegions") or [])


# Many descriptions carry a human place name that the SKU's serviceRegions
# already states -- "Hyperdisk Balanced Capacity in Iowa", "Standard Storage
# Hong Kong". Matching has to ignore it.
_PLACE_SUFFIX = re.compile(r"\s+(in\s+[A-Z].*|\(.*\))$")


def base_description(sku: dict[str, Any]) -> str:
    return _PLACE_SUFFIX.sub("", sku.get("description", "")).strip()


def flat_global(
    ctx: RefreshContext,
    service: str,
    descriptions: set[str],
    group: str | None = None,
) -> dict[str, float]:
    """``{"_value": price}`` for a SKU Google prices the same everywhere.

    Cloud NAT addresses, Artifact Registry storage and log retention are all
    published against the region ``global`` rather than against a list of
    regions, so there is one rate and no fallback to mark approximate.
    """
    for sku in ctx.skus(service):
        if group and sku.get("category", {}).get("resourceGroup") != group:
            continue
        if base_description(sku) not in descriptions:
            continue
        price = unit_price(sku)
        if price is not None:
            return {"_value": price}
    return {}


def prefixed_by_region(
    ctx: RefreshContext, service: str, prefix: str, group: str | None = None
) -> dict[str, float]:
    """``{region: price}`` for SKUs whose description starts with ``prefix``.

    Some families name the place in the middle of the description -- "Cloud
    Load Balancer Forwarding Rule Minimum for Hong Kong (asia-east2)" -- so
    there is no suffix to strip and the stable part is the front.
    """
    table: dict[str, float] = {}
    for sku in ctx.skus(service):
        if group and sku.get("category", {}).get("resourceGroup") != group:
            continue
        if not sku.get("description", "").startswith(prefix):
            continue
        price = unit_price(sku)
        if price is None:
            continue
        for region in regions_of(sku):
            table[region] = price
    return table


def by_region(
    ctx: RefreshContext,
    service: str,
    wanted: dict[str, str],
    group: str | None = None,
    family: str | None = None,
) -> dict[str, dict[str, float]]:
    """``{region: {variant: price}}`` for SKUs whose description is in ``wanted``.

    ``wanted`` maps an exact (place-suffix-stripped) description to the variant
    name the price table should key it under.
    """
    table: dict[str, dict[str, float]] = {}
    for sku in ctx.skus(service):
        category = sku.get("category", {})
        if group and category.get("resourceGroup") != group:
            continue
        if family and category.get("resourceFamily") != family:
            continue
        variant = wanted.get(base_description(sku))
        if variant is None:
            continue
        price = unit_price(sku)
        if price is None:
            continue
        for region in regions_of(sku):
            table.setdefault(region, {})[variant] = price
    return table


def flat_by_region(
    ctx: RefreshContext,
    service: str,
    descriptions: set[str],
    group: str | None = None,
) -> dict[str, float]:
    """``{region: price}`` for SKUs whose description is in ``descriptions``."""
    table: dict[str, float] = {}
    for sku in ctx.skus(service):
        if group and sku.get("category", {}).get("resourceGroup") != group:
            continue
        if base_description(sku) not in descriptions:
            continue
        price = unit_price(sku)
        if price is None:
            continue
        for region in regions_of(sku):
            table[region] = price
    return table


# --------------------------------------------------------------------------
# Core fetchers
# --------------------------------------------------------------------------

COMPUTE = "Compute Engine"

# Disk type (the last segment of a disk's `type` URL) to the catalog
# description that prices it. Regional (synchronously replicated) disks are a
# separate SKU at roughly double, and are keyed separately because a disk's
# own URL says which it is.
DISK_DESCRIPTIONS = {
    "Storage PD Capacity": "pd-standard",
    "Regional Storage PD Capacity": "regional-pd-standard",
    "Balanced PD Capacity": "pd-balanced",
    "Regional Balanced PD Capacity": "regional-pd-balanced",
    "SSD backed PD Capacity": "pd-ssd",
    "Regional SSD backed PD Capacity": "regional-pd-ssd",
    "Extreme PD Capacity": "pd-extreme",
    "Hyperdisk Balanced Capacity": "hyperdisk-balanced",
    "Hyperdisk Extreme Capacity": "hyperdisk-extreme",
    "Hyperdisk ML Capacity": "hyperdisk-ml",
    "Hyperdisk Throughput Capacity": "hyperdisk-throughput",
}


@price_fetcher("disk_gb_month", label="Persistent Disk and Hyperdisk capacity")
def fetch_disk_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: {disk type: usd per GB-month}}.

    Capacity only. Hyperdisk also bills provisioned IOPS and throughput as
    separate SKUs, so the unattached-disk check says so rather than quietly
    understating what a Hyperdisk costs.
    """
    return {"disk_gb_month": by_region(ctx, COMPUTE, DISK_DESCRIPTIONS, family="Storage")}


@price_fetcher(
    "snapshot_gb_month",
    "image_gb_month",
    label="Snapshot and custom image storage",
)
def fetch_snapshot_and_image_rates(ctx: RefreshContext) -> dict[str, Any]:
    """Snapshot and image storage, both billed per GB-month of stored data."""
    return {
        "snapshot_gb_month": flat_by_region(
            ctx, COMPUTE, {"Storage PD Snapshot"}, group="PDSnapshot"
        ),
        "image_gb_month": flat_by_region(ctx, COMPUTE, {"Storage Image"}, group="StorageImage"),
    }


@price_fetcher("static_ip_hour", label="Reserved static IP addresses")
def fetch_static_ip_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: usd per hour} for a reserved external IP attached to nothing.

    "Static Ip Charge" is specifically the idle rate. Google bills a reserved
    address that is *not* in use at a higher hourly rate than one attached to a
    running VM ("External IP Charge on a Standard VM"), which is the whole
    reason the unused-static-ip check is worth running.
    """
    return {"static_ip_hour": flat_by_region(ctx, COMPUTE, {"Static Ip Charge"}, group="IpAddress")}


NETWORKING = "Networking"


@price_fetcher("nat_ip_hour", label="Cloud NAT addresses")
def fetch_nat_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: usd per hour} for one IP held by a Cloud NAT gateway.

    Cloud NAT gateway uptime is billed per VM using it, so a gateway with no
    VMs behind it costs nothing for uptime -- unlike an AWS NAT gateway, which
    bills a flat hourly charge regardless. What an idle Cloud NAT does still
    cost is the external addresses it holds, which is what this rate prices.
    """
    return {
        "nat_ip_hour": flat_global(ctx, NETWORKING, {"Networking Cloud NAT IP Usage"}, group="Nat")
    }


@price_fetcher("forwarding_rule_hour", label="Load balancer forwarding rules")
def fetch_forwarding_rule_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: usd per hour} for the first five forwarding rules of a load balancer.

    Google bills a load balancer by its forwarding rules, at a flat minimum
    covering the first five, plus data processing. A load balancer with no
    healthy backend still pays the minimum, and that is what is wasted.
    """
    return {
        "forwarding_rule_hour": prefixed_by_region(
            ctx,
            NETWORKING,
            "Cloud Load Balancer Forwarding Rule Minimum",
            group="LoadBalancing",
        )
    }


@price_fetcher("sql_storage_gb_month", label="Cloud SQL storage")
def fetch_sql_storage_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: {ssd|hdd: usd per GB-month}} of Cloud SQL storage.

    Cloud SQL prices storage per engine, and the engines agree within a region,
    so the highest rate found for each storage class is taken rather than
    picking one engine's SKU and hoping the others match. Storage is what a
    stopped instance keeps paying for; vCPU and memory stop.
    """
    table: dict[str, dict[str, float]] = {}
    classes = {"SSD": "ssd", "PDStandard": "hdd"}
    for sku in ctx.skus("Cloud SQL"):
        group = sku.get("category", {}).get("resourceGroup")
        variant = classes.get(group)
        description = base_description(sku)
        if variant is None or not description.startswith("Cloud SQL for "):
            continue
        price = unit_price(sku)
        # Per-GB SKUs only: the same group also carries per-instance and
        # per-vCPU charges, which are priced per month rather than per GB.
        unit = (sku.get("pricingInfo") or [{}])[-1].get("pricingExpression", {}).get("usageUnit")
        if price is None or unit != "GiBy.mo" or price <= 0:
            continue
        for region in regions_of(sku):
            current = table.setdefault(region, {})
            current[variant] = max(current.get(variant, 0.0), price)
    return {"sql_storage_gb_month": table}


@price_fetcher("gcs_gb_month", label="Cloud Storage standard class")
def fetch_gcs_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: usd per GB-month} of Standard-class Cloud Storage."""
    table: dict[str, float] = {}
    for sku in ctx.skus("Cloud Storage"):
        if sku.get("category", {}).get("resourceGroup") != "RegionalStorage":
            continue
        description = sku.get("description", "")
        if not description.startswith("Standard Storage"):
            continue
        price = unit_price(sku)
        if price is None:
            continue
        for region in regions_of(sku):
            table[region] = price
    return {"gcs_gb_month": table}


@price_fetcher("dns_zone_month", label="Cloud DNS managed zones")
def fetch_dns_rates(ctx: RefreshContext) -> dict[str, Any]:
    """Managed zone price per tier. Global, and tiered by zones per account.

    Deleting one zone saves the rate of the tier the account is actually in:
    the first 25 zones cost more than the ones after them, so an account with
    thirty saves the second tier's rate, not the first's.
    """
    tiers: dict[str, float] = {}
    for sku in ctx.skus("Cloud DNS"):
        if base_description(sku) != "ManagedZone":
            continue
        for index, name in enumerate(("first_25", "next_9975", "additional")):
            price = usd(sku, tier=index)
            if price is not None:
                tiers[name] = price
    return {"dns_zone_month": tiers}


@price_fetcher("secret_version_month", label="Secret Manager versions")
def fetch_secret_rates(ctx: RefreshContext) -> dict[str, Any]:
    """Per active secret version replica, per month. Priced the same everywhere."""
    for sku in ctx.skus("Secret Manager"):
        if base_description(sku) == "Secret version replica storage":
            price = unit_price(sku)
            if price is not None:
                return {"secret_version_month": {"_value": price}}
    return {"secret_version_month": {}}


@price_fetcher("kms_key_version_month", label="Cloud KMS key versions")
def fetch_kms_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: {software|hsm: usd per month}} for one active key version.

    A disabled key version still bills as active until it is destroyed, which
    is the entire point of the disabled-kms-key check.
    """
    table: dict[str, dict[str, float]] = {}
    for sku in ctx.skus("Cloud Key Management Service (KMS)"):
        description = base_description(sku)
        if not description.startswith("Active ") or "key versions" not in description:
            continue
        variant = "hsm" if " HSM " in description else "software"
        price = unit_price(sku)
        if price is None:
            continue
        targets = regions_of(sku) or ([FALLBACK_REGION] if is_global(sku) else [])
        for region in targets:
            current = table.setdefault(region, {})
            # Several key algorithms share a tier at one price; the symmetric
            # software key is the cheapest and the most common, so the minimum
            # is the honest floor for "what this key version costs".
            current[variant] = min(current.get(variant, price), price)
    return {"kms_key_version_month": table}


@price_fetcher("artifact_gb_month", label="Artifact Registry storage")
def fetch_artifact_rates(ctx: RefreshContext) -> dict[str, Any]:
    """USD per GB-month of Artifact Registry storage, one rate for every region.

    Tier 0 of this SKU is the 0.5 GB Google gives away, priced at zero, so the
    rate wanted is the first tier that actually charges.
    """
    return {
        "artifact_gb_month": flat_global(ctx, "Artifact Registry", {"Artifact Registry Storage"})
    }


@price_fetcher("log_retention_gb_month", label="Cloud Logging retention")
def fetch_logging_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: usd per GB-month} of log retention beyond the free 30 days."""
    return {
        "log_retention_gb_month": flat_global(
            ctx, "Cloud Logging", {"Log Retention cost"}, group="Logs"
        )
    }


@price_fetcher("filestore_gb_month", label="Filestore capacity")
def fetch_filestore_rates(ctx: RefreshContext) -> dict[str, Any]:
    """{region: {tier: usd per GB-month}} of Filestore capacity.

    Filestore's descriptions name the tier in prose rather than in the
    category, so the tier is matched on the words Google uses for it.
    """
    tiers = {
        "basic-hdd": ("Filestore Capacity Standard",),
        "basic-ssd": ("Filestore Capacity Premium",),
        "zonal": ("Filestore Capacity Zonal",),
        "regional": ("Filestore Capacity Regional",),
        "enterprise": ("Filestore Capacity Enterprise",),
    }
    table: dict[str, dict[str, float]] = {}
    for sku in ctx.skus("Cloud Filestore"):
        description = sku.get("description", "")
        unit = (sku.get("pricingInfo") or [{}])[-1].get("pricingExpression", {}).get("usageUnit")
        if unit != "GiBy.mo":
            continue
        price = unit_price(sku)
        if price is None or price <= 0:
            continue
        for variant, prefixes in tiers.items():
            if any(description.startswith(prefix) for prefix in prefixes):
                for region in regions_of(sku):
                    current = table.setdefault(region, {})
                    current[variant] = min(current.get(variant, price), price)
                break
    return {"filestore_gb_month": table}


# --------------------------------------------------------------------------
# Building and writing the table
# --------------------------------------------------------------------------


def build_table(ctx: RefreshContext) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_meta": {
            "source": "Google Cloud Billing Catalog API, on-demand USD list prices",
            "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": "Regenerate with: python -m zombiescan.pricing.refresh",
            "packs": sorted({f.pack for f in FETCHERS}),
        },
        "fallback_region": FALLBACK_REGION,
        "hours_per_month": HOURS_PER_MONTH,
    }
    for fetcher in FETCHERS:
        print(f"fetching {fetcher.label}...")
        produced = fetcher.fn(ctx)
        unexpected = set(produced) - set(fetcher.sections)
        if unexpected:
            # A fetcher writing sections it never declared would silently
            # overwrite another pack's rates.
            raise ValueError(
                f"fetcher {fetcher.label!r} produced undeclared sections: {sorted(unexpected)}"
            )
        for section, data in produced.items():
            payload[section] = dict(sorted(data.items())) if isinstance(data, dict) else data
            print(f"  {section}: {len(data)} entries")
    return payload


def regressions(payload: dict[str, Any], existing: dict[str, Any]) -> list[str]:
    """Sections the new table would lose against the one already on disk.

    A fetcher that returns nothing is not a price of zero, it is a refresh
    that failed, and writing its result would silently make every finding it
    prices free. Checked against the previous table rather than against a
    hardcoded list, so a pack's sections are protected the moment it ships one.
    """
    problems = []
    for section, previous in sorted(existing.items()):
        if section.startswith("_") or not isinstance(previous, dict) or not previous:
            continue
        fresh = payload.get(section)
        if fresh is None:
            problems.append(f"{section}: gone (had {len(previous)} entries)")
        elif isinstance(fresh, dict) and not fresh:
            problems.append(f"{section}: empty (had {len(previous)} entries)")
    return problems


def catalog_client() -> Any:
    credentials, _ = google.auth.default(scopes=SCOPES)
    return googleapiclient.discovery.build(
        "cloudbilling", "v1", credentials=credentials, cache_discovery=False
    )


def service_index(billing: Any) -> dict[str, str]:
    """Display name to service id, for every service in the catalog."""
    index: dict[str, str] = {}
    request = billing.services().list(pageSize=200)
    while request is not None:
        response = request.execute()
        for service in response.get("services", []):
            index[service["displayName"]] = service["serviceId"]
        request = billing.services().list_next(request, response)
    return index


def main() -> None:
    # Pack fetchers only exist once their packs are imported.
    from zombiescan import packs

    packs.discover()

    billing = catalog_client()
    ctx = RefreshContext(billing=billing, services=service_index(billing))
    payload = build_table(ctx)

    existing = json.loads(TABLE_PATH.read_text()) if TABLE_PATH.exists() else {}
    problems = regressions(payload, existing)
    if problems:
        raise SystemExit(
            "refusing to write the price table -- this refresh would lose rates:\n  "
            + "\n  ".join(problems)
            + "\nThe table on disk is unchanged. A section that vanishes usually means "
            "its fetcher never registered, or Google reworded the SKU it matches on."
        )

    TABLE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {TABLE_PATH}")


if __name__ == "__main__":
    # `python -m zombiescan.pricing.refresh` executes this file a second time,
    # as __main__, with a FETCHERS list of its own. Packs register into the
    # canonical zombiescan.pricing.refresh copy, so running __main__'s own
    # main() would fetch core's sections and none of any pack's. Hand over to
    # the canonical module instead.
    from zombiescan.pricing.refresh import main as canonical_main

    canonical_main()
