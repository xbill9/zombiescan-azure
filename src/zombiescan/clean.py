"""Executing cleanup plans.

Everything here is built so that a dry run and a real run take the same path
and produce the same plan. ``apply`` gates one thing: whether a planned step
is sent to Azure. If the dry run is wrong, the real run is wrong in the same
way, which is the only way a preview is worth anything.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from zombiescan import azure
from zombiescan.azure import Arm, ArmError
from zombiescan.cleaners import CLEANERS, Step
from zombiescan.models import Finding, ScanContext
from zombiescan.pricing import PriceTable
from zombiescan.registry import CHECKS

PLANNED = "planned"
APPLIED = "applied"
SKIPPED = "skipped"
FAILED = "failed"
UNSUPPORTED = "unsupported"


@dataclass
class Outcome:
    finding: Finding
    steps: list[Step] = field(default_factory=list)
    status: str = PLANNED
    error: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def irreversible(self) -> bool:
        return any(s.irreversible for s in self.steps)

    @property
    def monthly_saving(self) -> float:
        return self.finding.monthly_cost if self.status == APPLIED else 0.0


def plan_for(arm: Arm, finding: Finding, pricing: PriceTable) -> Outcome:
    """Work out the calls that would resolve this finding. Makes no changes."""
    spec = CHECKS.get(finding.check)
    if spec is not None and spec.uncleanable:
        return Outcome(finding=finding, status=UNSUPPORTED, error=spec.uncleanable)

    planner = CLEANERS.get(finding.check)
    if planner is None:
        # A --from report can name a check this build does not have, because
        # the pack that produced it is not installed here. Say so: "no cleaner"
        # would send the operator looking for a bug that is really a missing
        # dependency.
        if spec is None:
            return Outcome(
                finding=finding,
                status=UNSUPPORTED,
                error=(
                    f"no check named {finding.check} is installed, so its findings "
                    "cannot be planned -- install the pack that produced this report"
                ),
            )
        return Outcome(
            finding=finding,
            status=UNSUPPORTED,
            error=f"no cleaner is implemented for {finding.check}",
        )

    ctx = ScanContext(arm=arm, subscription=finding.subscription, pricing=pricing)
    try:
        steps = list(planner(ctx, finding))
    except Exception as exc:  # noqa: BLE001 - planning must not abort the run
        return Outcome(
            finding=finding, status=FAILED, error=f"could not plan: {type(exc).__name__}: {exc}"
        )

    if not steps:
        return Outcome(finding=finding, status=UNSUPPORTED, error="nothing to do for this finding")
    return Outcome(finding=finding, steps=steps, status=PLANNED)


def apply_outcome(outcome: Outcome, arm: Arm) -> Outcome:
    """Execute a planned outcome, stopping that finding at its first failure.

    Steps within a finding are ordered and dependent -- snapshot before
    delete, detach before release -- so a failed step must not be followed by
    the destructive one that assumed it succeeded.
    """
    for step in outcome.steps:
        try:
            response = arm.request(
                step.method,
                step.path,
                azure.api_version(step.resource_type),
                body=step.body,
            )
        except ArmError as exc:
            outcome.status = FAILED
            outcome.error = f"{step.summary}: {azure.message_of(exc)}"
            return outcome
        except Exception as exc:  # noqa: BLE001
            outcome.status = FAILED
            outcome.error = f"{step.summary}: {type(exc).__name__}: {exc}"
            return outcome
        outcome.results.append(
            {
                "method": step.method,
                "path": step.path,
                "description": step.description,
                # Most ARM mutations are long-running: the call returns 202
                # with an empty body and a provisioning state to follow. Keep
                # the identifiers a reader would need to audit it, and drop
                # the rest.
                "returned": {
                    key: value
                    for key, value in (response or {}).items()
                    if key in ("id", "name", "status", "provisioningState")
                    and isinstance(value, (str, int, float, bool))
                },
            }
        )
    outcome.status = APPLIED
    return outcome


def audit_document(
    outcomes: list[Outcome], applied: bool, principal: str | None = None
) -> dict[str, Any]:
    """A record of what was done, for the person who asks later."""
    counted: dict[str, int] = {}
    for outcome in outcomes:
        counted[outcome.status] = counted.get(outcome.status, 0) + 1
    return {
        "schema_version": 1,
        "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "apply" if applied else "dry-run",
        "principal": principal,
        "counts": counted,
        "monthly_saving": round(sum(o.monthly_saving for o in outcomes), 2),
        "actions": [
            {
                "check": o.finding.check,
                "resource_id": o.finding.resource_id,
                "subscription": o.finding.subscription,
                "resource_group": o.finding.resource_group,
                "location": o.finding.location,
                "monthly_cost": round(o.finding.monthly_cost, 2),
                "status": o.status,
                "irreversible": o.irreversible,
                "error": o.error,
                "steps": [
                    {
                        "description": s.description,
                        "method": s.method,
                        "path": s.path,
                        "resource_type": s.resource_type,
                        "body": s.body,
                        "irreversible": s.irreversible,
                    }
                    for s in o.steps
                ],
                "results": o.results,
            }
            for o in outcomes
        ],
    }
