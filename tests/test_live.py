"""End-to-end smoke test against a real Azure subscription.

Excluded by default (``addopts = -m 'not live'``). Run it deliberately:

    ZOMBIESCAN_LIVE=1 uv run pytest -m live

Read-only, like everything else here, but it does make real API calls and it
shells out to ``az`` for a token.

Two of these check things the offline suite structurally cannot. Every pinned
``api-version`` is verified against what ARM actually accepts, because a
version that has been retired fails one check at scan time in a worker thread
and is counted as "missing" rather than raised. And the ``--yes`` list in
``helpers`` is verified against the installed Azure CLI, because the right
list is whatever that CLI accepts today, not what a comment remembers.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from zombiescan import azure, helpers
from zombiescan.engine import (
    load_packs,
    resolve_subscriptions,
    scan,
    select_checks,
    verify_credentials,
)
from zombiescan.pricing import PriceTable

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ZOMBIESCAN_LIVE") != "1",
        reason="set ZOMBIESCAN_LIVE=1 to run against a real subscription",
    ),
]


@pytest.fixture(scope="module")
def session():
    load_packs()
    return verify_credentials()


@pytest.fixture(scope="module")
def scanned(session):
    arm, _principal, default = session
    subscriptions = resolve_subscriptions(arm, (), False, default)
    return subscriptions, scan(arm, subscriptions, select_checks(()), PriceTable.load())


def test_credentials_resolve(session):
    _arm, principal, _default = session
    assert principal


def test_a_subscription_resolves(session):
    arm, _principal, default = session
    subscriptions = resolve_subscriptions(arm, (), False, default)
    assert subscriptions and all(isinstance(s, str) for s in subscriptions)


def test_scan_completes_and_prices_what_it_finds(scanned):
    subscriptions, result = scanned

    # The scan reached the subscription: not every pair may succeed, but they
    # must not all fail, or "no findings" says nothing.
    assert not result.completely_failed
    assert result.total_monthly_cost >= 0
    for finding in result.findings:
        assert finding.subscription in subscriptions
        assert finding.location
        assert finding.monthly_cost >= 0
        assert finding.remediation.startswith("az ")
        # Every finding must carry what it takes to act on it. A resource
        # group is not derivable from anything else in the row.
        assert finding.arm_id.startswith("/subscriptions/")
        assert finding.resource_group or finding.location == azure.GLOBAL


def test_the_scan_makes_no_mutating_call(scanned):
    """Every generated command is text. Nothing in a finding can run one."""
    _subscriptions, result = scanned
    for finding in result.findings:
        assert isinstance(finding.remediation, str)


def test_a_check_that_failed_did_not_fail_on_its_api_version(scanned):
    """An `InvalidResourceType` is a retired api-version, not a missing service.

    ARM reports it as a 404-shaped failure, which `classify` reads as
    "missing" and the engine counts as unavailable -- so the check silently
    stops running and the subscription looks that much cleaner.
    """
    _subscriptions, result = scanned
    broken = [e for e in result.errors if "InvalidResourceType" in e.message]
    assert broken == [], f"checks running against a retired api-version: {broken}"


def test_every_pinned_api_version_is_one_arm_accepts(session):
    """The pin is a decision, and this is what keeps it from going stale.

    Asked of the provider listing rather than by making each call, so it
    covers types this subscription has no resources of.
    """
    arm, _principal, default = session
    version = azure.API_VERSIONS["Microsoft.Resources/providers"]

    cached: dict[str, dict[str, list[str]]] = {}
    stale = []
    for resource_type, pinned in sorted(azure.API_VERSIONS.items()):
        namespace = azure.namespace_of(resource_type)
        if namespace in ("Microsoft.Resources", "Microsoft.ResourceGraph"):
            # Resource Manager's own surfaces are not listed as provider
            # resource types, so there is nothing to compare against.
            continue
        if namespace not in cached:
            listing = arm.request("GET", f"/subscriptions/{default}/providers/{namespace}", version)
            cached[namespace] = {
                entry["resourceType"].lower(): entry.get("apiVersions", [])
                for entry in listing.get("resourceTypes", [])
            }
        offered = cached[namespace].get(resource_type.split("/", 1)[1].lower(), [])
        if offered and pinned not in offered:
            newest = sorted(v for v in offered if "preview" not in v)
            stale.append(f"{resource_type}: pinned {pinned}, newest stable {newest[-1:]}")
    assert stale == [], "api-versions ARM no longer accepts:\n  " + "\n  ".join(stale)


def test_every_subscription_scanned_is_routed_to_a_tenant(session):
    """A scan can span tenants, and a token is issued for exactly one.

    If a subscription resolves to no tenant it falls back to the default
    token, which is another tenant's and will be refused -- so this is the
    check that the ``az`` subscription list and the routing agree.
    """
    arm, _principal, default = session
    subscriptions = resolve_subscriptions(arm, (), True, default)
    assert subscriptions, "the Azure CLI reports no enabled subscriptions"

    unrouted = [s for s in subscriptions if not arm.tenant_of(s)]
    assert unrouted == [], f"subscriptions with no tenant: {unrouted}"


def test_one_token_is_held_per_tenant_not_per_subscription(session):
    """The invariant `prepare` exists to keep: `az` is shelled out to once per
    tenant, before any worker runs, not once per subscription and not from
    inside the pool."""
    arm, _principal, default = session
    subscriptions = resolve_subscriptions(arm, (), True, default)
    arm.prepare(subscriptions)

    tenants = {arm.tenant_of(s) for s in subscriptions}
    held = {t for t in arm._credential.tenants if t}
    assert tenants <= held, f"tenants with no token: {sorted(tenants - held)}"


def test_the_confirm_list_matches_the_installed_azure_cli():
    """`az` has no global --quiet: --yes exists only where a command prompts.

    Passing it to a command that does not take one is an error rather than a
    no-op, so a generated plan built on a wrong list does not run. The right
    list is whatever the operator's `az` accepts.
    """
    wrong = []
    for command in sorted(helpers.CONFIRMS):
        help_text = subprocess.run(
            ["az", *command.split(), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout
        if "--yes" not in help_text:
            wrong.append(f"{command}: listed as prompting, but `az` offers no --yes")
    assert wrong == [], "\n".join(wrong)


def test_no_command_that_prompts_is_missing_from_the_confirm_list():
    """The other direction: a command that prompts and is not listed would
    make the generated plan stop and wait for an answer nobody is there to
    give."""
    import pathlib
    import re

    import zombiescan.packs

    # Every verb any check emits, taken from the commands themselves.
    verbs = set()
    root = pathlib.Path(zombiescan.packs.__file__).parent
    for module in sorted(root.glob("*/*.py")):
        for match in re.finditer(r'f?"(az [a-z][a-z0-9 -]*?) --', module.read_text()):
            verbs.add(match.group(1))

    missing = []
    for command in sorted(verbs):
        help_text = subprocess.run(
            [*command.split(), "--help"], capture_output=True, text=True, timeout=120
        ).stdout
        prompts = "--yes" in help_text
        listed = helpers._prompts(command)
        if prompts and not listed:
            missing.append(f"{command}: prompts, but is not in helpers.CONFIRMS")
    assert missing == [], "\n".join(missing)
