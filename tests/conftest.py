"""Shared test fixtures.

Tests run offline against recorded API shapes. The price table is pinned here
rather than loaded from the bundled one, so regenerating real prices
(`python -m zombiescan.pricing.refresh`) cannot break the assertions.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from zombiescan import engine
from zombiescan.models import ScanContext
from zombiescan.pricing import PriceTable

# Importing a check module registers that one check. The registry-wide tests
# ("every check has a cleaner or says why not") need every pack loaded, so do
# it once here rather than leaving it to whichever test imported what first.
engine.load_packs()

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

PROJECT = "test-project"

# Deliberately not the real table: round numbers make an expected cost
# readable in the test that asserts it. us-central1 and europe-west1 are
# priced; asia-south2 is not, so it exercises the fallback.
TEST_PRICES = {
    "fallback_region": "us-central1",
    "hours_per_month": 730,
    "disk_gb_month": {
        "us-central1": {
            "pd-standard": 0.04,
            "pd-balanced": 0.10,
            "pd-ssd": 0.17,
            "hyperdisk-balanced": 0.08,
        },
        "europe-west1": {"pd-standard": 0.044, "pd-balanced": 0.11, "pd-ssd": 0.187},
    },
    "snapshot_gb_month": {"us-central1": 0.05, "europe-west1": 0.055},
    "image_gb_month": {"us-central1": 0.05, "europe-west1": 0.05},
    "gcs_gb_month": {"us-central1": 0.02, "europe-west1": 0.023},
    "static_ip_hour": {"us-central1": 0.01, "europe-west1": 0.012},
    "forwarding_rule_hour": {"us-central1": 0.025, "europe-west1": 0.0275},
    "nat_ip_hour": {"_value": 0.005},
    "artifact_gb_month": {"_value": 0.10},
    "log_retention_gb_month": {"_value": 0.01},
    "secret_version_month": {"_value": 0.06},
    "sql_storage_gb_month": {
        "us-central1": {"ssd": 0.17, "hdd": 0.09},
        "europe-west1": {"ssd": 0.187, "hdd": 0.099},
    },
    "kms_key_version_month": {
        "us-central1": {"software": 0.06, "hsm": 1.00},
        "europe-west1": {"software": 0.066, "hsm": 1.10},
    },
    "filestore_gb_month": {"us-central1": {"zonal": 0.25, "regional": 0.45}},
    "dns_zone_month": {"first_25": 0.20, "next_9975": 0.10, "additional": 0.03},
    "gke_cluster_hour": {"_value": 0.10},
}


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded API response."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _resolve(spec: Any, kwargs: dict[str, Any]) -> Any:
    """A response spec is either a literal or a callable of the call's kwargs.

    The callable form is what lets one fake answer differently per argument --
    listing a backend service's health returns different targets for each one.
    """
    return spec(**kwargs) if callable(spec) else spec


class FakeRequest:
    def __init__(self, response: Any) -> None:
        self._response = response

    def execute(self) -> Any:
        return self._response


class FakeNode:
    """One level of a discovery client, e.g. ``compute.disks()``.

    Responses are keyed by the dotted path the production code uses:
    ``"disks.aggregatedList"``, ``"projects.secrets.list"``. Any name that is
    not a configured method is assumed to be a deeper collection, which is how
    the real client nests.
    """

    def __init__(self, client: FakeClient, prefix: str) -> None:
        self._client = client
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        key = f"{self._prefix}.{name}" if self._prefix else name

        # Pagination companion. One page per fixture keeps the fakes readable;
        # the paging loop itself is covered in test_shared.py.
        if name.endswith("_next"):
            return lambda request, response: None

        if key in self._client.responses:

            def call(**kwargs: Any) -> FakeRequest:
                self._client.record(key, kwargs)
                return FakeRequest(_resolve(self._client.responses[key], kwargs))

            return call

        # Not a method, so it must be a nested collection.
        return lambda: FakeNode(self._client, key)


class FakeClient(FakeNode):
    """Stands in for a discovery-built client.

    ``responses`` maps a dotted ``collection.method`` path to the response it
    returns, or to a callable taking the call's kwargs. Calls are recorded in
    ``calls`` (last kwargs per operation) and ``call_log`` (every call, in
    order) so tests can assert on server-side filtering.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: dict[str, dict[str, Any]] = {}
        self.call_log: list[tuple[str, dict[str, Any]]] = []
        super().__init__(self, "")

    def record(self, operation: str, kwargs: dict[str, Any]) -> None:
        self.calls[operation] = kwargs
        self.call_log.append((operation, kwargs))


@pytest.fixture
def pricing() -> PriceTable:
    return PriceTable(TEST_PRICES)


@pytest.fixture
def make_context(pricing: PriceTable):
    """Build a ScanContext wired to FakeClients, and expose them.

    Pass ``responses`` for a single-API check, or ``by_api`` when a check
    crosses two services and the test needs to configure each separately.
    """

    def _make(
        responses: dict[str, Any] | None = None,
        by_api: dict[str, dict[str, Any]] | None = None,
        project: str = PROJECT,
    ) -> tuple[ScanContext, FakeClient]:
        clients: dict[str, FakeClient] = {
            api: FakeClient(config) for api, config in (by_api or {}).items()
        }
        primary = FakeClient(responses) if responses is not None else None

        class FakeClients:
            def get(self, api: str) -> FakeClient:
                if api in clients:
                    return clients[api]
                if primary is not None:
                    return primary
                clients.setdefault(api, FakeClient({}))
                return clients[api]

        ctx = ScanContext(clients=FakeClients(), project=project, pricing=pricing)  # type: ignore[arg-type]
        return ctx, (primary if primary is not None else clients)

    return _make
