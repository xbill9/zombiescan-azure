"""Project fan-out and check execution.

Every Google Cloud call made from here is read-only. The engine never mutates
anything and never runs generated remediation.

The unit of fan-out is the project, not the region. ``aggregatedList`` and the
``locations/-`` wildcard cover every location in one call, so a scan of ten
projects makes roughly ten calls per check rather than ten times forty.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field

import googleapiclient.errors

from zombiescan import gcp, packs
from zombiescan.gcp import Clients, CredentialError
from zombiescan.models import Finding, ScanContext
from zombiescan.pricing import PriceTable
from zombiescan.registry import CHECKS, CheckSpec

__all__ = [
    "CredentialError",
    "ScanError",
    "ScanResult",
    "filter_locations",
    "load_packs",
    "resolve_projects",
    "scan",
    "select_checks",
    "verify_credentials",
]


@dataclass
class ScanError:
    project: str
    check: str
    message: str


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    attempted: int = 0
    # Pairs where the API is simply switched off on that project. A fact about
    # how the project is set up, not a failure: a project that has never used
    # Filestore has no Filestore waste, and an error per disabled API would
    # bury the findings under noise.
    unavailable: int = 0

    @property
    def total_monthly_cost(self) -> float:
        return sum(f.monthly_cost for f in self.findings)

    @property
    def completely_failed(self) -> bool:
        """Every project/check pair errored, so "no findings" means nothing.

        A scan of a project that does not exist reports zero waste and zero
        findings, which is indistinguishable from a clean project unless the
        caller is told the difference.
        """
        return self.attempted > 0 and (len(self.errors) + self.unavailable) == self.attempted


def verify_credentials(quota_project: str | None = None) -> tuple[Clients, str, str | None]:
    """Build clients from ADC. Returns ``(clients, principal, default project)``.

    The principal is whatever the credentials identify as -- a user email, or
    a service account -- and is recorded in the report so a later ``clean
    --from`` can refuse a report produced by somebody else.
    """
    credentials, project = gcp.default_credentials(quota_project)
    clients = Clients(credentials)
    principal = _principal_of(credentials)
    return clients, principal, project


def _principal_of(credentials) -> str:
    """Who these credentials belong to, without spending an API call.

    ADC user credentials carry no identity until they are refreshed, and a
    service account carries its email outright. Neither path needs a network
    round trip, which keeps `zombiescan checks` usable offline.
    """
    for attribute in ("service_account_email", "signer_email", "_account", "account"):
        value = getattr(credentials, attribute, None)
        if isinstance(value, str) and value:
            return value
    return "application default credentials"


def resolve_projects(
    clients: Clients,
    projects: tuple[str, ...],
    all_projects: bool,
    default_project: str | None,
) -> list[str]:
    """Which projects to scan.

    ``--all-projects`` asks Resource Manager for every ACTIVE project the
    caller can see, which is the real equivalent of an AWS ``--all-regions``:
    the forgotten resources are in the project nobody opens.
    """
    if projects:
        return list(projects)

    if all_projects:
        found = []
        for project in gcp.paginate(
            clients.get("cloudresourcemanager"),
            "projects",
            method="search",
            key="projects",
            query="state:ACTIVE",
        ):
            project_id = project.get("projectId")
            if project_id:
                found.append(project_id)
        if not found:
            raise CredentialError(
                "--all-projects found no active projects these credentials can see. "
                "Pass --project explicitly."
            )
        return sorted(found)

    if not default_project:
        raise CredentialError(
            "No project configured. Set one with 'gcloud config set project <id>', "
            "pass --project, or use --all-projects."
        )
    return [default_project]


def filter_locations(findings: list[Finding], locations: tuple[str, ...]) -> list[Finding]:
    """Keep only findings in the given zones or regions.

    Applied to findings rather than to the calls, because one aggregated call
    already returned every location. Naming a region keeps its zones too:
    ``--location us-central1`` is the question an operator means to ask, and
    it would be a trap for it to exclude ``us-central1-a``.
    """
    if not locations:
        return findings
    wanted = set(locations)
    return [
        f
        for f in findings
        if f.location in wanted or gcp.region_of(f.location) in wanted or f.location == gcp.GLOBAL
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


def _run_one(spec: CheckSpec, clients: Clients, project: str, pricing: PriceTable):
    ctx = ScanContext(clients=clients, project=project, pricing=pricing)
    return list(spec.fn(ctx))


def scan(
    clients: Clients,
    projects: list[str],
    checks: list[CheckSpec],
    pricing: PriceTable,
    max_workers: int = 16,
) -> ScanResult:
    jobs = [(spec, project) for project in projects for spec in checks]
    result = ScanResult(projects=list(projects), attempted=len(jobs))
    if not jobs:
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_one, spec, clients, project, pricing): (spec, project)
            for spec, project in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            spec, project = futures[future]
            try:
                result.findings.extend(future.result())
            except googleapiclient.errors.HttpError as exc:
                kind = gcp.classify(exc)
                if kind in ("disabled", "missing"):
                    # The API is off, or the project is gone. Nothing to find
                    # and nothing went wrong.
                    result.unavailable += 1
                elif kind == "forbidden":
                    result.errors.append(
                        ScanError(project, spec.name, "permission denied: no access")
                    )
                else:
                    result.errors.append(ScanError(project, spec.name, gcp.message_of(exc)))
            except Exception as exc:  # noqa: BLE001 - one bad project must not kill the scan
                result.errors.append(ScanError(project, spec.name, f"{type(exc).__name__}: {exc}"))

    result.findings.sort(key=lambda f: (-f.monthly_cost, f.project, f.location, f.resource_id))
    return result
