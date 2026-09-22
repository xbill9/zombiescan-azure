"""Shared test fixtures.

Tests run offline against recorded ARM shapes. The price table is pinned here
rather than loaded from the bundled one, so regenerating real prices
(`python -m zombiescan.pricing.refresh`) cannot break the assertions.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from zombiescan import engine
from zombiescan.azure import ArmError
from zombiescan.models import ScanContext
from zombiescan.pricing import PriceTable

# Importing a check module registers that one check. The registry-wide tests
# ("every check has a cleaner or says why not") need every pack loaded, so do
# it once here rather than leaving it to whichever test imported what first.
engine.load_packs()

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

SUBSCRIPTION = "00000000-1111-2222-3333-444444444444"
RESOURCE_GROUP = "test-rg"

# Deliberately not the real table: round numbers make an expected cost
# readable in the test that asserts it. eastus and westeurope are priced;
# southindia is not, so it exercises the fallback.
TEST_PRICES = {
    "fallback_region": "eastus",
    "hours_per_month": 730,
    # Azure prices a managed disk by tier, flat per month -- not per GiB.
    "disk_tier_month": {
        "eastus": {
            "S4 LRS": 1.50,
            "S10 LRS": 6.00,
            "E10 LRS": 10.00,
            "P1 LRS": 0.60,
            "P4 LRS": 3.00,
            "P10 LRS": 20.00,
            "P30 LRS": 135.00,
            "P10 ZRS": 25.00,
        },
        "westeurope": {"S4 LRS": 1.65, "P10 LRS": 22.00, "P30 LRS": 148.50},
    },
    # The two SKUs billed per provisioned GiB instead.
    "disk_gb_month": {
        "eastus": {"PremiumV2_LRS": 0.08, "UltraSSD_LRS": 0.12},
        "westeurope": {"PremiumV2_LRS": 0.088},
    },
    "snapshot_gb_month": {"eastus": 0.05, "westeurope": 0.055},
    "blob_gb_month": {"eastus": 0.02, "westeurope": 0.023},
    "public_ip_hour": {"eastus": 0.005, "westeurope": 0.006},
    # Published against "Global": one rate, no region layer.
    "nat_gateway_hour": {"_value": 0.045},
    "load_balancer_hour": {"_value": 0.025},
    "log_ingestion_gb": {"_value": 2.30},
    "log_retention_gb_month": {"_value": 0.10},
    "app_service_hour": {
        "eastus": {"P1 v3": 0.30, "P1 v3 linux": 0.15, "B1": 0.075, "S1": 0.10},
        "westeurope": {"P1 v3": 0.33},
    },
    "sql_storage_gb_month": {
        "eastus": {"general_purpose": 0.115, "business_critical": 0.25, "hyperscale": 0.25},
        "westeurope": {"general_purpose": 0.127},
    },
    "dns_zone_month": {"first_25": 0.50, "beyond_25": 0.10},
    "keyvault_key_month": {"software": 0.0, "hsm": 1.00, "hsm_advanced": 5.00},
    "acr_registry_day": {"Basic": 0.1666, "Standard": 0.6666, "Premium": 1.6666},
    "aks_cluster_hour": {
        "eastus": {"Free": 0.0, "Standard": 0.10, "Premium": 0.60},
        "westeurope": {"Free": 0.0, "Standard": 0.10},
    },
}


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded ARM response."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def arm_id(
    resource_type: str,
    name: str,
    group: str = RESOURCE_GROUP,
    subscription: str = SUBSCRIPTION,
) -> str:
    """The full ARM id of a resource, spelled the way ARM spells it."""
    return f"/subscriptions/{subscription}/resourceGroups/{group}/providers/{resource_type}/{name}"


def _resolve(spec: Any, **kwargs: Any) -> Any:
    """A response spec is either a literal or a callable of the call's arguments.

    The callable form is what lets one fake answer differently per parent --
    listing two Key Vaults' keys returns a different set for each.
    """
    return spec(**kwargs) if callable(spec) else spec


class FakeArm:
    """Stands in for the ARM client.

    ``responses`` maps the **tail of a request path** to the response it
    returns, which is how the real surface is addressed: a check asks for
    ``Microsoft.Compute/disks`` and a vault's keys come from a path ending in
    ``/keys``. The longest matching tail wins, so a specific sub-path can
    override the type it hangs off -- which matters because Key Vault's
    vaults, keys and secrets are all pinned to the same resource type.

    ``graph`` maps a substring of a KQL query to the rows it returns, because
    a test should say "the network interface query returns this" rather than
    repeat the query.

    Calls are recorded in ``calls`` (last arguments per path) and ``call_log``
    (every call, in order) so tests can assert on what was actually asked.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        graph: dict[str, Any] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.graph_responses = graph or {}
        self.calls: dict[str, dict[str, Any]] = {}
        self.call_log: list[tuple[str, str]] = []
        self.graph_log: list[str] = []

    # -- matching -----------------------------------------------------------

    def _match(self, path: str) -> Any:
        best: tuple[int, str] | None = None
        for key in self.responses:
            if path.endswith(key) and (best is None or len(key) > best[0]):
                best = (len(key), key)
        if best is None:
            return None
        return self.responses[best[1]]

    # -- the Arm surface ----------------------------------------------------

    def list(self, path: str, resource_type: str, key: str = "value", **query: Any):
        self.calls[path] = {"resource_type": resource_type, **query}
        self.call_log.append(("list", path))
        spec = self._match(path)
        if spec is None:
            return iter(())
        response = _resolve(spec, path=path)
        if isinstance(response, ArmError):
            raise response
        return iter(response.get(key) or [])

    def get(self, path: str, resource_type: str, **query: Any) -> dict[str, Any]:
        self.calls[path] = {"resource_type": resource_type, **query}
        self.call_log.append(("get", path))
        spec = self._match(path)
        if spec is None:
            # ARM answers a missing sub-resource with 404, and several checks
            # depend on telling that from an empty one.
            raise ArmError(404, "ResourceNotFound", f"nothing recorded for {path}", path)
        response = _resolve(spec, path=path)
        if isinstance(response, ArmError):
            raise response
        return response

    def graph(self, query: str, subscription: str):
        self.graph_log.append(query)
        for fragment, rows in self.graph_responses.items():
            if fragment in query:
                return iter(_resolve(rows, query=query))
        return iter(())

    def registered_providers(self, subscription: str) -> frozenset[str]:
        # Offline, every provider is treated as registered: the pre-check is
        # the engine's job and is tested in test_engine.py against a fake that
        # says otherwise.
        return frozenset({"Microsoft"})


@pytest.fixture
def pricing() -> PriceTable:
    return PriceTable(TEST_PRICES)


@pytest.fixture
def make_context(pricing: PriceTable):
    """Build a ScanContext wired to a FakeArm, and expose it."""

    def _make(
        responses: dict[str, Any] | None = None,
        graph: dict[str, Any] | None = None,
        subscription: str = SUBSCRIPTION,
        providers: frozenset[str] = frozenset(),
    ) -> tuple[ScanContext, FakeArm]:
        arm = FakeArm(responses, graph)
        ctx = ScanContext(
            arm=arm,  # type: ignore[arg-type]
            subscription=subscription,
            pricing=pricing,
            providers=providers,
        )
        return ctx, arm

    return _make
