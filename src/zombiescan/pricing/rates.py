"""Rate specifications: how a section of the price table becomes a number.

The table itself is data, but turning a section into a monthly USD figure
needs a few facts the JSON does not carry -- whether the stored rate is
hourly, whether the section is keyed by a variant (a disk type, a storage
class), and what to fall back to when the variant is unknown.

They live here, as ``RateSpec`` records in an open registry. A pack registers
the specs for its own sections at import time and then prices its findings
through ``ctx.pricing.rate(...)`` like anything else, so a pack can price
something core has never heard of without a method being added to
``PriceTable``.

Four shapes cover almost everything:

    flat        {region: price}                     -- blob storage
    hourly      {region: price}, x hours_per_month  -- public IP
    variants    {region: {variant: price}}          -- disks by tier
    global      price with no region at all         -- NAT Gateway

That last shape is not a rounding of the truth on Azure. A NAT Gateway, a
load balancer rule and a DNS zone are all published in the Retail Prices API
against ``armRegionName`` values that are not ARM regions at all -- "Global",
or a billing geography like "Zone 1" -- and have no per-region entry to fall
back to. A regional lookup for one of them finds nothing.

Anything stranger registers a resolver function with ``register_resolver``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from zombiescan.pricing import PriceTable

# A resolver gets the table and whatever keyword arguments the caller passed,
# and returns the same (price, approximate) pair every lookup returns.
Resolver = Callable[..., tuple[float, bool]]


@dataclass(frozen=True)
class RateSpec:
    """How to read one section of the price table."""

    key: str
    section: str
    # Stored rates are per hour and must be multiplied by hours_per_month.
    per_hour: bool = False
    # The section holds {region: {variant: price}} rather than {region: price}.
    variants: bool = False
    # Which variant to use when the requested one is missing. None means a
    # missing variant is unpriceable: return 0.0 marked approximate rather
    # than silently pricing it as something else.
    default_variant: str | None = None
    # "global" sections are a bare value with no region layer.
    scope: str = "regional"
    # Which pack registered this, for `zombiescan rates` and error messages.
    pack: str = "core"

    @property
    def is_global(self) -> bool:
        return self.scope == "global"


RATES: dict[str, RateSpec] = {}
RESOLVERS: dict[str, Resolver] = {}


def register_rate(spec: RateSpec) -> RateSpec:
    """Register a rate spec. Raises on a duplicate key.

    Duplicates are a bug rather than an override: two packs quietly claiming
    the same rate key would make the price of a finding depend on import
    order, which is the least debuggable failure this code could have.
    """
    if spec.key in RATES or spec.key in RESOLVERS:
        owner = RATES[spec.key].pack if spec.key in RATES else "a resolver"
        raise ValueError(f"duplicate rate key {spec.key!r} (already registered by {owner})")
    RATES[spec.key] = spec
    return spec


def register_resolver(key: str, fn: Resolver, pack: str = "core") -> Resolver:
    """Register a custom resolver for a rate whose shape ``RateSpec`` cannot express."""
    if key in RATES or key in RESOLVERS:
        raise ValueError(f"duplicate rate key {key!r}")
    RESOLVERS[key] = fn
    return fn


def resolve(table: PriceTable, key: str, **kwargs: Any) -> tuple[float, bool]:
    """Look up ``key`` in ``table``. Returns ``(usd_per_month, approximate)``."""
    if key in RESOLVERS:
        return RESOLVERS[key](table, **kwargs)
    spec = RATES.get(key)
    if spec is None:
        known = ", ".join(sorted(set(RATES) | set(RESOLVERS)))
        raise KeyError(f"unknown rate key {key!r}. Registered: {known}")
    return table.apply(spec, **kwargs)


def _register_core_rates() -> None:
    """The rate specs for the checks that ship in this package."""
    for spec in (
        # --- flat per-region storage rates ---------------------------------
        RateSpec("snapshot.gb_month", "snapshot_gb_month"),
        RateSpec("blob.gb_month", "blob_gb_month"),
        # --- hourly rates billed by uptime ---------------------------------
        RateSpec("public_ip.month", "public_ip_hour", per_hour=True),
        # --- global: one rate, no per-region entry to fall back to ---------
        # Azure publishes both of these against armRegionName "Global". A
        # NAT Gateway bills its hourly fee whether or not a single VM sits
        # behind it, which is the opposite of how Cloud NAT bills and the
        # same as an AWS NAT gateway.
        RateSpec("nat_gateway.month", "nat_gateway_hour", per_hour=True, scope="global"),
        RateSpec("load_balancer.month", "load_balancer_hour", per_hour=True, scope="global"),
        RateSpec("log.ingestion_gb", "log_ingestion_gb", scope="global"),
        RateSpec("log.retention_gb_month", "log_retention_gb_month", scope="global"),
        # --- keyed by a variant --------------------------------------------
        # A managed disk is billed by the tier its provisioned size falls in,
        # flat per month: a P10 costs the same whether it holds 1 GiB or 128.
        # There is no sensible default -- pricing an unknown tier as some
        # other tier would be wrong by up to three orders of magnitude -- so
        # an unrecognised one is reported as unpriced instead.
        RateSpec("disk.tier_month", "disk_tier_month", variants=True),
        # Premium SSD v2 and Ultra are the two SKUs billed per provisioned
        # GiB rather than by tier.
        RateSpec("disk.gb_month", "disk_gb_month", variants=True),
        # SQL Database's General Purpose tier is the default and by far the
        # most common.
        RateSpec(
            "sql.storage_gb_month",
            "sql_storage_gb_month",
            variants=True,
            default_variant="general_purpose",
        ),
        RateSpec("app_service.month", "app_service_hour", per_hour=True, variants=True),
        # A dedicated host bills per host whatever runs on it, keyed by
        # helpers.host_sku_key. No default: host prices run from under $1 to
        # over $50 an hour, and guessing one would be wrong by that much.
        RateSpec("dedicated_host.month", "dedicated_host_hour", per_hour=True, variants=True),
        # Provisioned throughput, per PTU-hour, keyed by the deployment's SKU
        # name: ProvisionedManaged, GlobalProvisionedManaged,
        # DataZoneProvisionedManaged. Billed whether or not a request arrives.
        RateSpec("ptu.month", "ptu_hour", per_hour=True, variants=True),
        # Container Apps rates, all stored per hour: idle_vcpu and idle_gib for
        # a Consumption replica that is running but not serving, and
        # dedicated_vcpu, dedicated_gib and dedicated_management for the
        # Dedicated plan.
        RateSpec("container_apps.month", "container_apps_hour", per_hour=True, variants=True),
        # --- global, no region layer ---------------------------------------
        # A software-protected key in a Standard vault is free: Key Vault
        # bills per operation, not per key. Only HSM-protected keys carry a
        # per-key monthly charge, which is why the disabled-key check prices
        # those and reports the rest as hygiene.
        RateSpec(
            "keyvault.key_month",
            "keyvault_key_month",
            variants=True,
            scope="global",
        ),
    ):
        register_rate(spec)

    # Container Registry bills a flat daily fee per registry by tier, with no
    # per-GB component until the tier's included storage runs out -- so an
    # empty Premium registry costs exactly as much as a full one. A rate
    # published per *day* needs a month of days rather than the month of
    # hours ``per_hour`` would give it, which is why this is a resolver and
    # not a spec.
    register_resolver("acr.registry_month", _acr_registry_month)
    # Linux pay-as-you-go compute by ARM size name. An unused capacity
    # reservation slot and a running ML compute node both bill at this rate.
    register_resolver("vm.month", _vm_month)


def _vm_month(table: Any, region: str = "", variant: str = "") -> tuple[float, bool]:
    """A VM size's monthly Linux compute rate, matching the size name in any case.

    Compute and capacity reservations spell a size ``Standard_DS3_v2``; Azure
    Machine Learning spells the same one ``STANDARD_DS3_V2``. The table keys
    it the first way.
    """
    sizes, approximate = table.lookup_section("vm_hour", region)
    if not sizes:
        return 0.0, True
    price = sizes.get(variant)
    if price is None:
        wanted = variant.lower()
        price = next((p for size, p in sizes.items() if size.lower() == wanted), None)
    if price is None:
        return 0.0, True
    return float(price) * table.hours_per_month, approximate


_DAYS_PER_MONTH = 730 / 24


def _acr_registry_month(table: Any, sku: str = "Basic") -> tuple[float, bool]:
    """A container registry's monthly tier fee, from a rate published per day."""
    rates = table.section("acr_registry_day") or {}
    price = rates.get(sku)
    if price is None:
        return 0.0, True
    return float(price) * _DAYS_PER_MONTH, False


_register_core_rates()


def _dns_zone(table: Any, zone_count: int = 1) -> tuple[float, bool]:
    """What deleting one public DNS zone actually saves.

    Azure DNS bills zones in tiers -- the first 25 cost $0.50/month each and
    the ones after them $0.10 -- so the saving is the rate of the tier the
    subscription is actually in, not the headline first-tier price. A
    subscription with thirty zones saves the second tier's rate by deleting
    one.

    The zone rate has no ARM region. Azure publishes it against a billing
    geography spelled "Zone 1", which shares a word with availability zones
    and means something else entirely, so it is stored as a global section.
    """
    tiers = table.section("dns_zone_month") or {}
    if not tiers:
        return 0.0, True
    if zone_count > 25 and "beyond_25" in tiers:
        return float(tiers["beyond_25"]), False
    if "first_25" in tiers:
        return float(tiers["first_25"]), False
    return float(next(iter(tiers.values()))), True


def _register_irregular_rates() -> None:
    register_resolver("dns.zone_month", _dns_zone)


_register_irregular_rates()
