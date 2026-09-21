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

    flat        {region: price}                     -- Artifact Registry
    hourly      {region: price}, x hours_per_month  -- static IP
    variants    {region: {variant: price}}          -- disks by type
    global      price with no region at all         -- Cloud DNS zones

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
        RateSpec("image.gb_month", "image_gb_month"),
        RateSpec("gcs.gb_month", "gcs_gb_month"),
        # --- hourly rates billed by uptime ---------------------------------
        RateSpec("static_ip.month", "static_ip_hour", per_hour=True),
        RateSpec("forwarding_rule.month", "forwarding_rule_hour", per_hour=True),
        # --- global: Google charges one rate everywhere --------------------
        RateSpec("artifact.gb_month", "artifact_gb_month", scope="global"),
        RateSpec("log.retention_gb_month", "log_retention_gb_month", scope="global"),
        RateSpec("nat_ip.month", "nat_ip_hour", per_hour=True, scope="global"),
        # --- keyed by a variant --------------------------------------------
        # An unknown disk type is priced as pd-balanced, which has been the
        # default for `gcloud compute disks create` since 2021.
        RateSpec("disk.gb_month", "disk_gb_month", variants=True, default_variant="pd-balanced"),
        # Cloud SQL defaults to SSD storage and most instances never change it.
        RateSpec(
            "sql.storage_gb_month", "sql_storage_gb_month", variants=True, default_variant="ssd"
        ),
        # A software key version is the common case; HSM keys are opt-in.
        RateSpec(
            "kms.key_version_month",
            "kms_key_version_month",
            variants=True,
            default_variant="software",
        ),
        RateSpec("filestore.gb_month", "filestore_gb_month", variants=True),
        # --- global, no region layer ---------------------------------------
        RateSpec("secret.version_month", "secret_version_month", scope="global"),
    ):
        register_rate(spec)


_register_core_rates()


def _dns_zone(table: Any, zone_count: int = 1) -> tuple[float, bool]:
    """What deleting one managed zone actually saves.

    Cloud DNS bills zones in tiers -- the first 25 cost $0.20/month each and
    the ones after them less -- so the saving is the rate of the tier the
    project is actually in, not the headline first-tier price. An account with
    thirty zones saves the second tier's rate by deleting one.
    """
    tiers = table.section("dns_zone_month") or {}
    if not tiers:
        return 0.0, True
    if zone_count > 10000 and "additional" in tiers:
        return float(tiers["additional"]), False
    if zone_count > 25 and "next_9975" in tiers:
        return float(tiers["next_9975"]), False
    if "first_25" in tiers:
        return float(tiers["first_25"]), False
    return float(next(iter(tiers.values()))), True


def _register_irregular_rates() -> None:
    register_resolver("dns.zone_month", _dns_zone)


_register_irregular_rates()
