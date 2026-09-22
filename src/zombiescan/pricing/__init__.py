"""Bundled Azure price table.

Prices ship with the package so a scan works offline and does not add a
Retail Prices API call (and its latency) to every run. Regenerate with
``uv run python -m zombiescan.pricing.refresh``.

Lookups are keyed by *region*, as ARM spells one: ``eastus``, not ``East US``.
``Finding.region`` and ``azure.region_of`` normalise it. Azure bills the same
rate in every availability zone of a region, so a zone never enters a lookup.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from zombiescan.pricing.rates import RateSpec, resolve

_TABLE_PATH = pathlib.Path(__file__).with_name("table.json")


class PriceTable:
    """Region-aware lookups with an explicit fallback.

    Any lookup that falls back to the default region returns
    ``approximate=True`` so the report can mark the number rather than
    quietly presenting a guess as fact.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self._fallback = data.get("fallback_region", "eastus")
        self._hours = data.get("hours_per_month", 730)

    @classmethod
    def load(cls) -> PriceTable:
        return cls(json.loads(_TABLE_PATH.read_text()))

    @property
    def generated(self) -> str:
        return self._data.get("_meta", {}).get("generated", "unknown")

    def _lookup(self, section: str, region: str) -> tuple[Any, bool]:
        table = self._data.get(section, {})
        if region in table:
            return table[region], False
        return table.get(self._fallback), True

    @property
    def hours_per_month(self) -> int:
        """Hours the table bills a month as. Azure bills 730."""
        return self._hours

    def section(self, name: str) -> Any:
        """One raw section of the table, or None. For resolvers with odd shapes."""
        return self._data.get(name)

    def lookup_section(self, name: str, region: str) -> tuple[Any, bool]:
        """A section's entry for ``region``, falling back to the default region."""
        return self._lookup(name, region)

    def _monthly(self, value: float, spec: RateSpec) -> float:
        return value * self._hours if spec.per_hour else value

    def apply(self, spec: RateSpec, region: str | None = None, variant: str | None = None):
        """Read one section of the table according to its ``RateSpec``."""
        if spec.is_global:
            rates = self._data.get(spec.section) or {}
            if spec.variants:
                if variant not in rates:
                    # Same rule as the regional branch: a variant the table has
                    # never heard of is unpriced, not free.
                    return 0.0, True
                return self._monthly(rates[variant], spec), False
            value = rates if not isinstance(rates, dict) else rates.get("_value", 0.0)
            return self._monthly(float(value or 0.0), spec), not bool(rates)

        value, approximate = self._lookup(spec.section, region or self._fallback)

        if spec.variants:
            if not value:
                return 0.0, True
            if variant in value:
                return self._monthly(value[variant], spec), approximate
            # Unknown variant: fall back if the spec names one, and mark the
            # number approximate either way -- it is not the rate that was asked
            # for. With no fallback the rate is simply unknown, and reporting
            # zero silently would hide the finding's cost rather than flag it.
            if spec.default_variant and spec.default_variant in value:
                return self._monthly(value[spec.default_variant], spec), True
            return 0.0, True

        if not value:
            # An hourly section with no rate cannot be multiplied out, so the
            # answer is explicitly a guess. A flat section keeps the lookup's
            # own verdict, which is the long-standing behaviour of these rates.
            return (0.0, True) if spec.per_hour else (0.0, approximate)
        return self._monthly(value, spec), approximate

    def rate(self, key: str, **kwargs: Any) -> tuple[float, bool]:
        """Look up a registered rate by key. Returns ``(usd_per_month, approximate)``.

        This is the entry point a pack prices through: every rate any pack has
        registered is reachable here, so a pack never needs a method added to
        this class.
        """
        return resolve(self, key, **kwargs)
