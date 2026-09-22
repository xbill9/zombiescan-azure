"""Engine scheduling: which checks run where, and how failures are counted."""

from __future__ import annotations

import pytest

from tests.conftest import TEST_PRICES
from zombiescan.azure import ArmError
from zombiescan.engine import ScanResult, filter_locations, scan, select_checks
from zombiescan.models import Finding
from zombiescan.pricing import PriceTable
from zombiescan.registry import CheckSpec

SUBSCRIPTIONS = ["sub-a", "sub-b", "sub-c"]


def _finding(check="c", subscription="sub-a", location="eastus", cost=1.0):
    return Finding(
        check=check,
        resource_id=f"{check}-1",
        resource_type="t",
        subscription=subscription,
        location=location,
        reason="r",
        monthly_cost=cost,
        remediation="az noop",
    )


def _spec(name, seen, raises=None, providers=("Microsoft.Compute",), arm=None):
    def fn(ctx):
        seen.append((name, ctx.subscription))
        if arm is not None:
            arm.checks_run += 1
        if raises is not None:
            raise raises
        yield _finding(name, ctx.subscription)

    return CheckSpec(name=name, title=name, fn=fn, providers=providers)


class FakeArm:
    """An ARM stand-in whose registered providers the test chooses.

    It also records the order of `prepare` against the checks that ran, which
    is what the token-warming invariant is asserted on.
    """

    def __init__(self, registered=("Microsoft.Compute",), tenants=None):
        self.registered = frozenset(registered)
        self.registration_calls = 0
        self.prepared: list[str] | None = None
        self.prepared_before_any_check = None
        self._tenants = tenants or {}
        self.checks_run = 0

    def registered_providers(self, subscription):
        self.registration_calls += 1
        return self.registered

    def prepare(self, subscriptions):
        self.prepared = list(subscriptions)
        self.prepared_before_any_check = self.checks_run == 0

    def tenant_of(self, subscription):
        return self._tenants.get(subscription, "")


@pytest.fixture
def pricing():
    return PriceTable(TEST_PRICES)


def test_a_check_runs_once_per_subscription(pricing):
    """The unit of fan-out is the subscription: one list call covers the regions."""
    seen = []
    result = scan(FakeArm(), SUBSCRIPTIONS, [_spec("one", seen)], pricing)
    assert sorted(s for _, s in seen) == sorted(SUBSCRIPTIONS)
    assert result.attempted == 3
    assert len(result.findings) == 3
    assert result.completely_failed is False


def test_a_check_is_not_run_where_its_provider_is_not_registered(pricing):
    """ARM would answer with an empty page, and the scan would read as clean.

    This is the pre-check that keeps a false all-clear from being possible,
    so it is asserted on rather than left to the client layer.
    """
    seen = []
    spec = _spec("app", seen, providers=("Microsoft.Web",))
    result = scan(FakeArm(registered=("Microsoft.Compute",)), ["sub-a"], [spec], pricing)

    assert seen == [], "the check ran against a provider that is not registered"
    assert result.unavailable == 1
    assert result.errors == []
    assert result.findings == []


def test_provider_namespaces_match_regardless_of_case(pricing):
    """ARM lists ``microsoft.insights`` for a check that declares ``Microsoft.Insights``."""
    seen = []
    spec = _spec("webtests", seen, providers=("Microsoft.Insights",))
    result = scan(FakeArm(registered=("microsoft.insights",)), ["sub-a"], [spec], pricing)

    assert seen == [("webtests", "sub-a")]
    assert result.unavailable == 0


def test_an_unregistered_provider_is_not_an_error(pricing):
    result = scan(
        FakeArm(registered=()),
        SUBSCRIPTIONS,
        [_spec("one", [])],
        pricing,
    )
    assert result.unavailable == 3
    assert result.errors == []
    # Nothing could be scanned, so "no findings" is not an all-clear.
    assert result.completely_failed is True


def test_a_permission_failure_is_reported_as_one(pricing):
    denied = ArmError(403, "AuthorizationFailed", "does not have authorization")
    result = scan(FakeArm(), ["sub-a"], [_spec("one", [], raises=denied)], pricing)
    assert result.unavailable == 0
    assert [e.message for e in result.errors] == ["permission denied: no access"]
    assert result.errors[0].subscription == "sub-a"


def test_a_missing_subscription_is_not_an_error(pricing):
    gone = ArmError(404, "SubscriptionNotFound", "not found")
    result = scan(FakeArm(), ["sub-a"], [_spec("one", [], raises=gone)], pricing)
    assert result.unavailable == 1
    assert result.errors == []


def test_an_unexpected_failure_does_not_take_the_scan_with_it(pricing):
    seen = []
    checks = [_spec("bad", seen, raises=RuntimeError("boom")), _spec("good", seen)]
    result = scan(FakeArm(), SUBSCRIPTIONS, checks, pricing)
    assert len(result.findings) == 3
    assert len(result.errors) == 3
    assert "RuntimeError: boom" in result.errors[0].message


def test_the_registration_lookup_is_cached_by_the_client(pricing):
    """One call per subscription answers for every check, not one per pair."""
    arm = FakeArm()
    scan(arm, ["sub-a"], [_spec(f"c{n}", []) for n in range(5)], pricing)
    # The engine asks once per pair; the caching is the client's job, and the
    # fake counts every ask so the contract is visible here.
    assert arm.registration_calls == 5


def test_findings_come_back_costliest_first(pricing):
    def spec(name, cost):
        return CheckSpec(
            name=name,
            title=name,
            fn=lambda ctx, c=cost, n=name: iter([_finding(n, ctx.subscription, cost=c)]),
            providers=("Microsoft.Compute",),
        )

    result = scan(FakeArm(), ["sub-a"], [spec("cheap", 1.0), spec("dear", 99.0)], pricing)
    assert [f.check for f in result.findings] == ["dear", "cheap"]
    assert result.total_monthly_cost == 100.0


# --- location filtering ----------------------------------------------------


def test_a_region_filter_keeps_only_that_region():
    findings = [_finding(location="eastus"), _finding(location="westeurope")]
    assert [f.location for f in filter_locations(findings, ("eastus",))] == ["eastus"]


def test_a_region_filter_ignores_case_and_spaces():
    """`East US` is how the portal writes what ARM calls `eastus`."""
    findings = [_finding(location="eastus")]
    assert len(filter_locations(findings, ("East US",))) == 1


def test_global_findings_survive_a_region_filter():
    """A DNS zone has no region, so naming one is not a reason to hide it."""
    findings = [_finding(location="global"), _finding(location="westeurope")]
    kept = filter_locations(findings, ("eastus",))
    assert [f.location for f in kept] == ["global"]


def test_no_filter_keeps_everything():
    findings = [_finding(location="eastus"), _finding(location="westeurope")]
    assert filter_locations(findings, ()) == findings


# --- check selection -------------------------------------------------------


def test_an_unknown_check_name_lists_the_real_ones():
    with pytest.raises(ValueError, match="unknown check"):
        select_checks(("no-such-check",))


def test_an_empty_scan_is_not_a_failed_one():
    assert ScanResult().completely_failed is False


# --- one token per tenant, all of them before the pool starts --------------


def test_every_tenant_in_scope_gets_a_token_before_any_worker_runs(pricing):
    """The whole credential design rests on this.

    A scan that spans two tenants needs a token for each, and minting the
    second lazily from inside the pool would put two `az` processes on the
    MSAL token cache at the same moment.
    """
    arm = FakeArm()
    seen = []
    scan(arm, SUBSCRIPTIONS, [_spec("one", seen, arm=arm)], pricing)

    assert arm.prepared == SUBSCRIPTIONS
    assert arm.prepared_before_any_check is True


def test_an_empty_scan_does_not_bother_fetching_tokens(pricing):
    arm = FakeArm()
    scan(arm, [], [_spec("one", [])], pricing)
    assert arm.prepared is None
