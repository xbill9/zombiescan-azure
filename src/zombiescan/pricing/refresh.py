"""Regenerate ``table.json`` from the Azure Retail Prices API.

Run this, not your memory, when a price looks wrong:

    uv run python -m zombiescan.pricing.refresh

Reads ``prices.azure.com``, which is public: no credentials, no subscription,
no ``az login``. Prices are pay-as-you-go USD list prices, so they ignore
reservations, savings plans, Azure Hybrid Benefit, dev/test rates and
enterprise agreements.

**How a meter becomes a rate.** Every row the API returns carries a
``serviceName``, a ``productName``, a ``skuName``, a free-text ``meterName``,
an ``armRegionName``, a ``unitOfMeasure`` and one price. Nothing in it says
"this is what an unattached P10 disk costs you". So each fetcher below states
the exact service, product and meter it matches, and a matcher that stops
matching yields an empty section -- which ``main`` refuses to write over a
populated one, rather than quietly pricing every disk at zero.

Four traps, all of which fail silently and all of which cost a rebuild to
find. Every one was measured against the live API rather than recalled:

1. **``priceType`` must be ``Consumption``.** The same meter is published as
   ``Reservation`` and ``DevTestConsumption`` too, at a fraction of the price.
   Reading one of those prices a finding at a rate the operator is not paying.
   ``ROWS`` filters on it and nothing else should have to.

2. **A tier's rows come back in no particular order, and the first tier is
   often free.** Log Analytics ingestion is published as $0.00 up to 5 GB and
   $2.30 after it; Key Vault HSM keys as $5.00, then $2.50, then $0.90, then
   $0.40 -- returned in the order 5.00, 0.90, 2.50, 0.40. Taking the first row
   records whichever tier the API happened to list first, and taking the one
   at ``tierMinimumUnits`` zero records the free allowance. ``unit_price``
   sorts by ``tierMinimumUnits`` and takes the first tier that charges.

3. **The meter name distinguishes the capacity charge from its neighbours.**
   ``P80 LRS Disk`` is $3,604.11 a month. ``P80 LRS Disk Mount`` is $219.00
   and ``P80 LRS Disk Operations`` is fractions of a cent. All three carry
   the same ``skuName``, and matching on the SKU alone picks one at random --
   a sixteen-fold understatement that looks like a working price table.

4. **Some meters have no ARM region at all.** NAT Gateway and Load Balancer
   are published against ``armRegionName`` "Global"; Azure DNS against a
   billing geography spelled "Zone 1", which shares a word with availability
   zones and means something unrelated. A per-region fetcher returns an empty
   section for all three. They go in a global section instead.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

TABLE_PATH = pathlib.Path(__file__).with_name("table.json")

RETAIL_API = "https://prices.azure.com/api/retail/prices"

# The catalog prices a month of a per-month meter directly, but hourly meters
# (NAT Gateway, load balancer rules, App Service plans) need a month's worth
# of hours. Azure bills a 730-hour month.
HOURS_PER_MONTH = 730

FALLBACK_REGION = "eastus"

# Rows whose armRegionName is one of these are not ARM regions. "Global" is
# how Azure publishes a meter that costs the same everywhere; the rest are
# sovereign clouds and edge zones that a commercial-cloud scan never prices
# against.
GLOBAL_REGION = "Global"


@dataclass
class RefreshContext:
    """What a fetcher is handed: a filtered reader over the Retail Prices API.

    There is no client to build and no token to hold. The API is public, so a
    fetcher's only tool is a query, and stating the query in the fetcher is
    what makes a rate auditable -- anyone can paste the filter into a browser
    and see the same rows.
    """

    verbose: bool = True

    def rows(self, *conditions: str) -> list[dict[str, Any]]:
        """Every pay-as-you-go row matching an OData filter.

        ``priceType eq 'Consumption'`` is added to every query here rather
        than left to each fetcher, because forgetting it is the mistake that
        prices a finding at a three-year reserved rate.
        """
        clauses = ["priceType eq 'Consumption'", *conditions]
        return list(_page(" and ".join(clauses)))


# The Retail Prices API throttles with HTTP 429. The VM section alone is about
# seventy pages, so a refresh reaches the limit often enough to need a retry.
THROTTLE_RETRIES = 6


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def _fetch_page(url: str) -> dict[str, Any]:
    """One page, waiting out a 429 with the server's ``Retry-After`` or a backoff."""
    for attempt in range(THROTTLE_RETRIES):
        try:
            return _get_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            wait = exc.headers.get("Retry-After") if exc.headers else None
            time.sleep(float(wait) if wait and wait.isdigit() else 2 ** (attempt + 2))
    return _get_json(url)


def _page(filter_expression: str) -> Iterator[dict[str, Any]]:
    """Every row of one Retail Prices query, following ``NextPageLink``."""
    url: str | None = f"{RETAIL_API}?$filter={urllib.parse.quote(filter_expression)}"
    while url:
        payload = _fetch_page(url)
        yield from payload.get("Items") or []
        url = payload.get("NextPageLink")


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
# Reading one meter
# --------------------------------------------------------------------------


def unit_price(rows: list[dict[str, Any]]) -> float | None:
    """What a meter actually charges per unit, skipping any free allowance.

    Azure publishes a tiered meter as several rows that differ only in
    ``tierMinimumUnits`` and ``retailPrice``, **in no guaranteed order**. The
    first 5 GB of Log Analytics ingestion is a row priced at zero; the first
    25 Azure DNS record sets are another. Reading whichever row came back
    first records an arbitrary tier, and reading the one at tier zero records
    the giveaway -- which does not fail loudly. It silently prices every
    finding in the section at nothing, and the scan reports a clean
    subscription.

    So the rate is the lowest tier that actually charges. A meter that is
    genuinely free at every tier returns 0.0, and an empty list returns None.
    """
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: float(row.get("tierMinimumUnits") or 0))
    for row in ordered:
        price = float(row.get("retailPrice") or 0)
        if price:
            return price
    return 0.0


def by_region(
    rows: list[dict[str, Any]],
    meter: Callable[[dict[str, Any]], str | None],
) -> dict[str, dict[str, float]]:
    """``{region: {variant: price}}``, with each variant's tiers read together.

    ``meter`` names the variant a row belongs to, or returns None to drop it.
    Rows are grouped before pricing so that a tiered meter is read as a whole
    rather than one row at a time -- which is the only way ``unit_price`` can
    tell a free allowance from a rate.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        variant = meter(row)
        region = row.get("armRegionName") or ""
        if not variant or not region or region == GLOBAL_REGION:
            continue
        grouped.setdefault((region, variant), []).append(row)

    table: dict[str, dict[str, float]] = {}
    for (region, variant), tiers in grouped.items():
        price = unit_price(tiers)
        if price is not None:
            table.setdefault(region, {})[variant] = price
    return table


def flat_by_region(
    rows: list[dict[str, Any]], keep: Callable[[dict[str, Any]], bool]
) -> dict[str, float]:
    """``{region: price}`` for the rows ``keep`` accepts."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        region = row.get("armRegionName") or ""
        if not region or region == GLOBAL_REGION or not keep(row):
            continue
        grouped.setdefault(region, []).append(row)
    table: dict[str, float] = {}
    for region, tiers in grouped.items():
        price = unit_price(tiers)
        if price is not None:
            table[region] = price
    return table


def flat_global(rows: list[dict[str, Any]], keep: Callable[[dict[str, Any]], bool]) -> dict:
    """``{"_value": price}`` for a meter Azure prices the same everywhere.

    NAT Gateway, load balancer rules and Azure DNS zones are all published
    without an ARM region, so there is one rate and no fallback to mark
    approximate.
    """
    matching = [row for row in rows if keep(row)]
    price = unit_price(matching)
    return {"_value": price} if price is not None else {}


def meter_is(row: dict[str, Any], suffix: str) -> bool:
    """Whether a row's meter is its SKU followed by exactly ``suffix``.

    This is what separates ``P80 LRS Disk`` from ``P80 LRS Disk Mount`` and
    ``P80 LRS Disk Operations``: all three carry ``skuName`` "P80 LRS", and
    only the first is the capacity charge.
    """
    return row.get("meterName") == f"{row.get('skuName')} {suffix}"


# --------------------------------------------------------------------------
# Core fetchers
# --------------------------------------------------------------------------

STORAGE = "serviceName eq 'Storage'"

# The three managed-disk families billed by tier. "Premium Page Blob"
# publishes the same P-tier meters for *unmanaged* disks at the same prices
# and is excluded, because a row from it would key a managed-disk rate off a
# product that is not what the check found.
TIERED_DISK_PRODUCTS = (
    "Standard HDD Managed Disks",
    "Standard SSD Managed Disks",
    "Premium SSD Managed Disks",
)


@price_fetcher("disk_tier_month", label="managed disk capacity, by tier")
def fetch_disk_tiers(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {"P10 LRS": usd per month}}``.

    Azure bills a managed disk by the tier its provisioned size falls into,
    flat, per month -- not per gigabyte. A 1 GiB Premium disk and a 128 GiB
    one are both a P10 and both cost $19.71 in ``eastus``. Pricing them per GB
    the way every other cloud works would understate a small disk by two
    orders of magnitude.
    """
    products = " or ".join(f"productName eq '{p}'" for p in TIERED_DISK_PRODUCTS)
    rows = ctx.rows(f"({products})", "unitOfMeasure eq '1/Month'")
    return {
        "disk_tier_month": by_region(
            rows, lambda row: row.get("skuName") if meter_is(row, "Disk") else None
        )
    }


# The two SKUs billed per provisioned GiB instead of by tier. Both also bill
# provisioned IOPS and throughput as separate meters, which is why the
# unattached-disk check says its figure is capacity only.
PER_GB_DISK_PRODUCTS = {"Azure Premium SSD v2": "PremiumV2_LRS", "Ultra Disks": "UltraSSD_LRS"}


@price_fetcher("disk_gb_month", label="Premium SSD v2 and Ultra capacity")
def fetch_disk_per_gb(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {sku: usd per GiB-month}}`` for the per-GiB disk families.

    Published per GiB-*hour*, so the rate is multiplied out here rather than
    by a ``per_hour`` spec -- the section also holds nothing else that would
    want the same treatment.
    """
    products = " or ".join(f"productName eq '{p}'" for p in PER_GB_DISK_PRODUCTS)
    rows = ctx.rows(f"({products})", "unitOfMeasure eq '1 GiB/Hour'")

    def variant(row: dict[str, Any]) -> str | None:
        if not str(row.get("meterName") or "").endswith("Provisioned Capacity"):
            return None
        return PER_GB_DISK_PRODUCTS.get(row.get("productName") or "")

    hourly = by_region(rows, variant)
    return {
        "disk_gb_month": {
            region: {sku: price * HOURS_PER_MONTH for sku, price in skus.items()}
            for region, skus in hourly.items()
        }
    }


@price_fetcher("snapshot_gb_month", label="managed disk snapshot storage")
def fetch_snapshot_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: usd per GB-month}`` of snapshot storage.

    Standard HDD LRS is the cheapest snapshot storage and the default a
    snapshot lands on, so it is the rate a snapshot is priced at. The figure
    is an upper bound for a second and later snapshot of the same disk:
    incremental snapshots bill only the blocks that changed, and nothing in a
    list call says how many those are.
    """
    rows = ctx.rows(
        "productName eq 'Standard HDD Managed Disks'",
        "unitOfMeasure eq '1 GB/Month'",
    )
    return {
        "snapshot_gb_month": flat_by_region(
            rows, lambda row: "Snapshots" in str(row.get("meterName") or "")
        )
    }


@price_fetcher("blob_gb_month", label="hot blob storage")
def fetch_blob_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: usd per GB-month}`` of locally redundant hot block blob storage."""
    rows = ctx.rows(
        STORAGE,
        "productName eq 'General Block Blob v2'",
        "skuName eq 'Hot LRS'",
        "unitOfMeasure eq '1 GB/Month'",
    )
    return {
        "blob_gb_month": flat_by_region(
            rows, lambda row: row.get("meterName") == "Hot LRS Data Stored"
        )
    }


@price_fetcher("public_ip_hour", label="static public IP addresses")
def fetch_public_ip_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: usd per hour}`` for a Standard static public IPv4 address.

    Azure charges the same hourly rate whether the address is attached to
    something or not, which differs from Google -- a reserved Google address
    costs *more* idle than in use. What makes an Azure address waste is simply
    that it is still reserved.
    """
    rows = ctx.rows("serviceName eq 'Virtual Network'", "productName eq 'IP Addresses'")
    return {
        "public_ip_hour": flat_by_region(
            rows, lambda row: row.get("meterName") == "Standard IPv4 Static Public IP"
        )
    }


# Spot and low-priority rows share a VM size's armSkuName and cost a fraction
# of the pay-as-you-go rate; either one read as the size's price would
# understate every finding priced from it.
DISCOUNTED_SKU = ("Spot", "Low Priority")

# Windows rows add the licence. Most products spell it "Windows"; a few GPU
# series abbreviate it to "Srs Win", which a filter on the full word misses.
WINDOWS_PRODUCT = re.compile(r"\bWin(dows)?\b")


def _linux_pay_as_you_go(row: dict[str, Any]) -> bool:
    return not WINDOWS_PRODUCT.search(row.get("productName") or "") and not any(
        word in (row.get("skuName") or "") for word in DISCOUNTED_SKU
    )


@price_fetcher("vm_hour", label="VM compute, Linux pay-as-you-go, by size")
def fetch_vm_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {vm size: usd per hour}}``, keyed by the ARM size name.

    What an unused capacity reservation slot costs: Azure bills one at the
    pay-as-you-go rate of its VM size. The Linux rate, because a reservation
    carries no operating system licence.

    ``startswith(productName,'Virtual Machines')`` keeps the VM products and
    drops Cloud Services and dedicated hosts, which publish the same
    armSkuName at other prices. The largest section in the table: about
    seventy thousand rows across seventy-odd regions.
    """
    rows = ctx.rows(
        "serviceName eq 'Virtual Machines'",
        "startswith(productName,'Virtual Machines')",
        "contains(productName,'Windows') eq false",
        "contains(skuName,'Spot') eq false",
        "contains(skuName,'Low Priority') eq false",
    )
    return {
        "vm_hour": by_region(
            rows,
            lambda row: row.get("armSkuName") if _linux_pay_as_you_go(row) else None,
        )
    }


@price_fetcher("dedicated_host_hour", label="Dedicated Host, by host SKU")
def fetch_dedicated_host_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {host sku key: usd per hour}}``.

    Azure bills a dedicated host per host, whatever runs on it. The Retail
    Prices API spells host SKUs inconsistently -- ``Dsv3_Type3``,
    ``Fsv2 Type3``, ``Ebsv5-Type1`` against ARM's ``DSv3-Type3`` -- so both
    sides are keyed by ``helpers.host_sku_key``, letters and digits only.

    A row is keyed by its armSkuName and, where that differs, by its skuName
    too, because the two disagree for a few families (``Mmsv2MedMem-Type1``
    against ``Msmv2MedMem Type1``). A skuName that is only ``Type 1``, with no
    family in it, is not used: it would collide across every family.
    """
    from zombiescan.helpers import host_sku_key

    rows = [
        row
        for row in ctx.rows(
            "serviceName eq 'Virtual Machines'", "contains(productName,'Dedicated Host')"
        )
        if _linux_pay_as_you_go(row)
    ]
    table = by_region(rows, lambda row: host_sku_key(row.get("armSkuName") or "") or None)
    aliases = by_region(
        rows,
        lambda row: (
            key
            if (key := host_sku_key(row.get("skuName") or "")) and not key.startswith("type")
            else None
        ),
    )
    for region, prices in aliases.items():
        for key, price in prices.items():
            table.setdefault(region, {}).setdefault(key, price)
    return {"dedicated_host_hour": table}


# The meter each provisioned deployment SKU bills against, per PTU-hour.
PTU_METERS = {
    "Provisioned Managed Regional Unit": "ProvisionedManaged",
    "Provisioned Managed Global Unit": "GlobalProvisionedManaged",
    "Provisioned Managed Data Zone Unit": "DataZoneProvisionedManaged",
}


@price_fetcher("ptu_hour", label="Foundry provisioned throughput, per PTU")
def fetch_ptu_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {deployment sku: usd per PTU-hour}}``.

    A provisioned deployment bills every PTU every hour, whether or not a
    request arrives. Regional, Data Zone and Global provisioned are separate
    meters at different prices -- $2.00, $1.10 and $1.00 in eastus -- so each
    is keyed by the deployment SKU name ARM reports. The monthly "Provisioned
    Throughput Units" rows are reservations and are not read.
    """
    rows = ctx.rows("serviceName eq 'Foundry Models'", "productName eq 'Azure OpenAI'")
    return {
        "ptu_hour": by_region(
            rows,
            lambda row: (
                PTU_METERS.get(row.get("meterName") or "")
                if row.get("unitOfMeasure") == "1/Hour"
                else None
            ),
        )
    }


# Container Apps meters, and the per-hour multiplier for the unit each is
# published in. Idle Consumption usage is per second; Dedicated is per hour.
CONTAINER_APPS_METERS = {
    "Standard vCPU Idle Usage": ("idle_vcpu", {"1 Second": 3600}),
    "Standard Memory Idle Usage": ("idle_gib", {"1 GiB Second": 3600}),
    "Dedicated vCPU Usage": ("dedicated_vcpu", {"1 Hour": 1}),
    "Dedicated Memory Usage": ("dedicated_gib", {"1 Hour": 1}),
    "Dedicated Plan Management": ("dedicated_management", {"1 Hour": 1}),
}


@price_fetcher("container_apps_hour", label="Container Apps idle and Dedicated rates")
def fetch_container_apps_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {rate: usd per hour}}`` for the charges an idle app still pays.

    A Consumption replica kept alive by ``minReplicas`` bills the idle vCPU
    and memory rates while it serves nothing. A Dedicated workload profile
    bills its instances' vCPU and memory by the hour, plus a management fee
    per environment, whatever runs on it. Every rate is stored per hour, so
    the per-second idle meters are multiplied out here.
    """
    rows = ctx.rows("serviceName eq 'Azure Container Apps'")
    table: dict[str, dict[str, float]] = {}
    for meter, (variant, units) in CONTAINER_APPS_METERS.items():
        matching = [row for row in rows if row.get("meterName") == meter]
        for region, prices in by_region(matching, lambda row: row.get("unitOfMeasure")).items():
            for unit, price in prices.items():
                if unit in units:
                    table.setdefault(region, {})[variant] = price * units[unit]
    return {"container_apps_hour": table}


@price_fetcher("nat_gateway_hour", label="NAT Gateway uptime")
def fetch_nat_rates(ctx: RefreshContext) -> dict[str, Any]:
    """USD per hour of NAT Gateway uptime, charged whether or not anything uses it.

    This is the finding most likely to surprise someone arriving from Google
    Cloud. Cloud NAT bills gateway uptime per VM behind it, so an idle one
    costs almost nothing; an Azure NAT Gateway bills a flat hourly fee the way
    an AWS NAT gateway does, so an idle one costs the full $32.85 a month.

    Published against the region "Global" with no per-region entry at all.
    """
    rows = ctx.rows("serviceName eq 'NAT Gateway'")
    return {
        "nat_gateway_hour": flat_global(
            rows, lambda row: row.get("meterName") == "Standard Gateway"
        )
    }


@price_fetcher("load_balancer_hour", label="Standard Load Balancer rules")
def fetch_load_balancer_rates(ctx: RefreshContext) -> dict[str, Any]:
    """USD per hour for a Standard Load Balancer's included rules.

    Azure bills a Standard load balancer a flat hourly rate covering its first
    five rules, plus data processed. A load balancer with an empty backend
    pool still pays the hourly rate, and that is what is wasted. Published
    against "Global".
    """
    rows = ctx.rows("serviceName eq 'Load Balancer'")
    return {
        "load_balancer_hour": flat_global(
            rows,
            lambda row: row.get("meterName") == "Standard Included LB Rules and Outbound Rules",
        )
    }


@price_fetcher("app_service_hour", label="App Service plan instances")
def fetch_app_service_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {sku: usd per hour}}`` per App Service plan instance.

    Azure prices the same plan SKU differently on Linux and on Windows -- a
    P1 v3 is $0.315/hour on Windows and about half that on Linux -- and
    publishes them as separate products. Both are kept, with the Linux rate
    keyed as ``"<sku> linux"``, because the plan's own ``reserved`` flag says
    which one a finding should be priced at and guessing would be wrong half
    the time.
    """
    rows = ctx.rows("serviceName eq 'Azure App Service'", "unitOfMeasure eq '1 Hour'")

    def variant(row: dict[str, Any]) -> str | None:
        if not meter_is(row, "App"):
            return None
        sku = str(row.get("skuName") or "")
        linux = str(row.get("productName") or "").endswith("Linux")
        return f"{sku} linux" if linux else sku

    return {"app_service_hour": by_region(rows, variant)}


# SQL Database publishes storage per service tier. General Purpose is the
# default and the overwhelming majority of databases.
SQL_STORAGE_METERS = {
    "General Purpose Data Stored": "general_purpose",
    "Business Critical Data Stored": "business_critical",
    "Hyperscale Data Stored": "hyperscale",
}


@price_fetcher("sql_storage_gb_month", label="SQL Database storage")
def fetch_sql_storage_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{region: {tier: usd per GB-month}}`` of SQL Database storage.

    Storage is what a paused database keeps paying for; its compute stops.
    Backup storage is a separate meter and is deliberately not included -- a
    paused database's backups are retained whatever happens to the database,
    so they are not freed by acting on this finding.
    """
    rows = ctx.rows("serviceName eq 'SQL Database'", "unitOfMeasure eq '1 GB/Month'")
    return {
        "sql_storage_gb_month": by_region(
            rows, lambda row: SQL_STORAGE_METERS.get(str(row.get("meterName") or ""))
        )
    }


@price_fetcher("dns_zone_month", label="Azure DNS public zones")
def fetch_dns_rates(ctx: RefreshContext) -> dict[str, Any]:
    """Public zone price per tier. Tiered by zones per subscription.

    Deleting one zone saves the rate of the tier the subscription is actually
    in: the first 25 zones cost $0.50 each and the ones after them $0.10, so a
    subscription with thirty saves the second tier's rate, not the first's.

    Azure publishes these against a *billing geography* -- ``armRegionName``
    is the literal string "Zone 1" -- which is not an ARM region and is
    unrelated to availability zones. Zone 1 is the commercial-cloud rate.
    """
    rows = ctx.rows("serviceName eq 'Azure DNS'", "meterName eq 'Public Zone'")
    zone_one = [row for row in rows if row.get("armRegionName") == "Zone 1"]
    tiers: dict[str, float] = {}
    for row in sorted(zone_one, key=lambda r: float(r.get("tierMinimumUnits") or 0)):
        minimum = float(row.get("tierMinimumUnits") or 0)
        name = "first_25" if minimum == 0 else "beyond_25"
        tiers.setdefault(name, float(row.get("retailPrice") or 0))
    return {"dns_zone_month": tiers}


# Key Vault charges per key only for HSM-protected keys. A software-protected
# key in a Standard vault is free -- the vault bills per operation instead --
# which is why the disabled-key check prices the HSM ones and reports the rest
# as hygiene.
KEY_VAULT_METERS = {
    "Premium HSM-protected RSA 2048-bit key": "hsm",
    "Premium HSM-protected Advanced Key": "hsm_advanced",
}


@price_fetcher("keyvault_key_month", label="Key Vault HSM-protected keys")
def fetch_key_vault_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{variant: usd per key per month}``. Priced the same in every region.

    The advanced-key meter is the clearest illustration of the tier trap in
    the whole catalog: it is published as $5.00, $0.90, $2.50 and $0.40 in
    that order, tiered at 0, 1500, 250 and 4000 keys. Sorting by
    ``tierMinimumUnits`` is the only way to read $5.00 as the first tier.
    """
    rows = ctx.rows("serviceName eq 'Key Vault'", "skuName eq 'Premium'")
    table: dict[str, float] = {}
    for meter, variant in KEY_VAULT_METERS.items():
        price = unit_price([row for row in rows if row.get("meterName") == meter])
        if price is not None:
            table[variant] = price
    # A software key genuinely costs nothing per month. Recording it is what
    # keeps the check from marking every software key's price approximate.
    table["software"] = 0.0
    return {"keyvault_key_month": table}


ACR_TIERS = ("Basic", "Standard", "Premium")


@price_fetcher("acr_registry_day", label="Container Registry tier fee")
def fetch_acr_rates(ctx: RefreshContext) -> dict[str, Any]:
    """``{tier: usd per day}`` for one registry.

    Container Registry bills a flat daily fee by tier with storage included up
    to a limit, so an empty Premium registry costs exactly what a full one
    does. The commercial-cloud rate is the lowest published for each tier; the
    higher ones are sovereign clouds.
    """
    rows = ctx.rows("serviceName eq 'Container Registry'", "unitOfMeasure eq '1/Day'")
    table: dict[str, float] = {}
    for tier in ACR_TIERS:
        prices = [
            float(row.get("retailPrice") or 0)
            for row in rows
            if row.get("meterName") == f"{tier} Registry Unit"
            and float(row.get("retailPrice") or 0) > 0
        ]
        if prices:
            table[tier] = min(prices)
    return {"acr_registry_day": table}


@price_fetcher(
    "log_ingestion_gb",
    "log_retention_gb_month",
    label="Log Analytics ingestion and retention",
)
def fetch_log_analytics_rates(ctx: RefreshContext) -> dict[str, Any]:
    """Log Analytics ingestion per GB, and retention per GB-month.

    Ingestion is the trap Google's catalog has too: the first 5 GB a month is
    published as a row priced at zero, and reading it records log ingestion as
    free. The rate wanted is the first tier that charges.
    """
    rows = ctx.rows("serviceName eq 'Log Analytics'", "armRegionName eq 'eastus'")

    def meter(name: str) -> dict:
        price = unit_price([row for row in rows if row.get("meterName") == name])
        return {"_value": price} if price is not None else {}

    return {
        "log_ingestion_gb": meter("Analytics Logs Data Ingestion"),
        "log_retention_gb_month": meter("Analytics Logs Data Retention"),
    }


# --------------------------------------------------------------------------
# Building and writing the table
# --------------------------------------------------------------------------


def build_table(ctx: RefreshContext) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_meta": {
            "source": "Azure Retail Prices API, pay-as-you-go USD list prices",
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


def main() -> None:
    # Pack fetchers only exist once their packs are imported.
    from zombiescan import packs

    packs.discover()

    payload = build_table(RefreshContext())

    existing = json.loads(TABLE_PATH.read_text()) if TABLE_PATH.exists() else {}
    problems = regressions(payload, existing)
    if problems:
        raise SystemExit(
            "refusing to write the price table -- this refresh would lose rates:\n  "
            + "\n  ".join(problems)
            + "\nThe table on disk is unchanged. A section that vanishes usually means "
            "its fetcher never registered, or Microsoft reworded the meter it matches on."
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
