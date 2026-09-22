"""Subscription fan-out and check execution.

Every Azure call made from here is read-only. The engine never mutates
anything and never runs generated remediation.

The unit of fan-out is the subscription, not the region. One Resource Graph
query covers every region and every resource group, so a scan of ten
subscriptions makes roughly ten calls per check rather than ten times sixty.

**A check is not run where its resource provider is not registered.** ARM
answers a list call against an unregistered provider with HTTP 200 and an
empty page, so a check that simply ran would find nothing and the subscription
would be reported clean. The registration is read once per subscription and
the pair is counted as unavailable instead -- the same treatment a disabled
API gets, for the same reason.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field

from zombiescan import azure, packs
from zombiescan.azure import Arm, ArmError, CredentialError, ProviderNotRegistered
from zombiescan.models import Finding, ScanContext
from zombiescan.pricing import PriceTable
from zombiescan.registry import CHECKS, CheckSpec

__all__ = [
    "CredentialError",
    "ScanError",
    "ScanResult",
    "filter_locations",
    "load_packs",
    "resolve_subscriptions",
    "scan",
    "select_checks",
    "verify_credentials",
]


@dataclass
class ScanError:
    subscription: str
    check: str
    message: str


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)
    attempted: int = 0
    # Pairs where the resource provider is simply not registered on that
    # subscription. A fact about how the subscription is set up, not a
    # failure: a subscription that has never used App Service has no App
    # Service waste, and an error per unregistered provider would bury the
    # findings under noise.
    unavailable: int = 0

    @property
    def total_monthly_cost(self) -> float:
        return sum(f.monthly_cost for f in self.findings)

    @property
    def completely_failed(self) -> bool:
        """Every subscription/check pair errored, so "no findings" means nothing.

        A scan of a subscription that does not exist reports zero waste and
        zero findings, which is indistinguishable from a clean subscription
        unless the caller is told the difference.
        """
        return self.attempted > 0 and (len(self.errors) + self.unavailable) == self.attempted


def verify_credentials(
    subscription: str | None = None, refresh: bool = False
) -> tuple[Arm, str, str | None]:
    """Build an ARM client from the ``az`` CLI's credentials.

    Returns ``(arm, principal, default subscription)``. The principal is
    whatever ``az`` is signed in as -- a user, or a service principal -- and is
    recorded in the report so a later ``clean --from`` can refuse a report
    produced by somebody else.

    The client is handed every subscription ``az`` knows about, across every
    tenant it has signed into, so that each request can be sent with a token
    issued for the right one. ``refresh`` re-queries each tenant rather than
    reading the CLI's cached list, which is worth the second it costs when the
    scan is about to sweep everything.
    """
    credential, principal, default = azure.default_credentials(subscription)
    return Arm(credential, azure.known_subscriptions(refresh=refresh)), principal, default


def resolve_subscriptions(
    arm: Arm,
    subscriptions: tuple[str, ...],
    all_subscriptions: bool,
    default_subscription: str | None,
) -> list[str]:
    """Which subscriptions to scan.

    ``--all-subscriptions`` takes every enabled subscription the ``az`` CLI
    holds credentials for, **across every tenant it has signed into**. That is
    the real equivalent of an AWS ``--all-regions``: the forgotten resources
    are in the subscription nobody opens, and often in the tenant nobody opens.

    Asking ARM instead would quietly cover one tenant, because an ARM token is
    issued for a single tenant and can only list that tenant's subscriptions.
    One person having two is ordinary -- Microsoft lets one email address be
    both a work or school account and a personal Microsoft account -- and a
    sweep that silently skipped half of them would report less waste and look
    like good news.
    """
    if subscriptions:
        return list(subscriptions)

    if all_subscriptions:
        found = sorted(s.id for s in arm.known if s.enabled)
        if not found:
            raise CredentialError(
                "--all-subscriptions found no enabled subscriptions the Azure CLI has "
                "signed into. Run 'az login' (add --allow-no-subscriptions if the account "
                "has none), or pass --subscription explicitly."
            )
        return found

    if not default_subscription:
        raise CredentialError(
            "No subscription configured. Set one with 'az account set --subscription <id>', "
            "pass --subscription, or use --all-subscriptions."
        )
    return [default_subscription]


def filter_locations(findings: list[Finding], locations: tuple[str, ...]) -> list[Finding]:
    """Keep only findings in the given regions.

    Applied to findings rather than to the queries, because one Resource Graph
    call already returned every region. Global resources are kept whatever is
    asked for: a DNS zone has no region to match, and hiding it because the
    operator named one would drop a finding they were not excluding.

    Region names are matched as ARM spells them -- ``eastus``, not ``East
    US`` -- but the comparison ignores case and spaces, so both work.
    """
    if not locations:
        return findings
    wanted = {azure.region_of(location) for location in locations}
    return [
        f for f in findings if azure.region_of(f.location) in wanted or f.location == azure.GLOBAL
    ]


def load_packs(disabled: frozenset[str] = frozenset()) -> packs.LoadReport:
    """Import every enabled pack, registering its checks, cleaners and rates.

    Call this before ``select_checks``: until a pack is imported, none of its
    checks exist to be selected.
    """
    return packs.discover(disabled=disabled)


def select_checks(
    names: tuple[str, ...], disabled_packs: frozenset[str] = frozenset()
) -> list[CheckSpec]:
    """The checks to run, honouring --check and --disable-pack.

    ``disabled_packs`` is applied here as well as at import time. Not loading a
    pack is what normally keeps its checks out of the registry, but a pack
    already imported by something else in the process would otherwise slip
    through -- and a flag that silently does nothing is worse than no flag.
    """
    if not names:
        return [spec for spec in CHECKS.values() if spec.pack not in disabled_packs]

    unknown = sorted(set(names) - set(CHECKS))
    if unknown:
        available = ", ".join(sorted(CHECKS))
        raise ValueError(f"unknown check(s): {', '.join(unknown)}. Available: {available}")

    selected = [CHECKS[n] for n in names]
    contradicted = sorted({s.name for s in selected if s.pack in disabled_packs})
    if contradicted:
        # Asking for a check and disabling its pack in the same command is a
        # mistake worth naming, not one to resolve by guessing which the
        # operator meant.
        raise ValueError(
            f"check(s) {', '.join(contradicted)} belong to a pack disabled by "
            "--disable-pack; drop one of the two flags"
        )
    return selected


def _run_one(spec: CheckSpec, arm: Arm, subscription: str, pricing: PriceTable):
    """One check against one subscription, refusing to run it blind.

    The registration lookup is cached per subscription, so this costs one call
    for a whole scan rather than one per pair.
    """
    registered = arm.registered_providers(subscription)
    known = {p.lower() for p in registered}
    missing = [p for p in spec.providers if p.lower() not in known]
    if missing:
        raise ProviderNotRegistered(
            f"{', '.join(missing)} is not registered on this subscription, so "
            f"{spec.name} has nothing to read. Register it with "
            f"'az provider register --namespace {missing[0]}' if that is wrong."
        )
    ctx = ScanContext(arm=arm, subscription=subscription, pricing=pricing, providers=registered)
    return list(spec.fn(ctx))


def scan(
    arm: Arm,
    subscriptions: list[str],
    checks: list[CheckSpec],
    pricing: PriceTable,
    max_workers: int = 16,
) -> ScanResult:
    jobs = [(spec, subscription) for subscription in subscriptions for spec in checks]
    result = ScanResult(subscriptions=list(subscriptions), attempted=len(jobs))
    if not jobs:
        return result

    # One token per tenant in scope, fetched here, on this thread, before any
    # worker exists. Minting them lazily from inside the pool would put two
    # `az` processes on the MSAL token cache at the same moment, which is the
    # failure this whole credential design is arranged to avoid.
    arm.prepare(subscriptions)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_one, spec, arm, subscription, pricing): (spec, subscription)
            for spec, subscription in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            spec, subscription = futures[future]
            try:
                result.findings.extend(future.result())
            except (ArmError, ProviderNotRegistered) as exc:
                kind = azure.classify(exc)
                if kind in ("unregistered", "missing"):
                    # The provider is not switched on, or the subscription is
                    # gone. Nothing to find and nothing went wrong.
                    result.unavailable += 1
                elif kind == "forbidden":
                    result.errors.append(
                        ScanError(subscription, spec.name, "permission denied: no access")
                    )
                else:
                    result.errors.append(ScanError(subscription, spec.name, azure.message_of(exc)))
            except Exception as exc:  # noqa: BLE001 - one bad subscription must not kill the scan
                result.errors.append(
                    ScanError(subscription, spec.name, f"{type(exc).__name__}: {exc}")
                )

    result.findings.sort(key=lambda f: (-f.monthly_cost, f.subscription, f.location, f.resource_id))
    return result
