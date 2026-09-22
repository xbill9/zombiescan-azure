"""Turning findings into the API calls that remove them.

A cleaner does not execute anything. It *plans*: given a finding, it yields
the mutating calls that would resolve it, in order. Read-only calls needed to
build that plan (fetching a VM's disks, reading a vault's soft-delete setting)
happen during planning, because a dry run that cannot see what it would touch
is not a dry run.

The runner in ``clean.py`` decides whether to execute the plan. That split is
the whole safety design: --apply changes one thing, whether the planned steps
are performed, and nothing about which steps get planned.

This module is only the registry. The cleaners themselves live beside the
checks they clean, in each pack's ``cleaners.py``, so a pack ships the removal
plan for its own findings rather than asking core to carry it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from zombiescan.models import Finding, ScanContext


@dataclass(frozen=True)
class Step:
    """One mutating ARM call.

    Every Azure mutation is an HTTP verb against a resource path, so a step is
    that pair plus the resource type its api-version is pinned from. Naming
    the call rather than closing over it is what lets a dry run print the
    exact request it would send -- ``DELETE /subscriptions/.../disks/scratch``
    is something an operator can check against the portal before agreeing to
    it.
    """

    description: str
    method: str
    path: str
    resource_type: str
    body: dict[str, Any] | None = None
    # Irreversible means no recovery window, no snapshot, no undo: once this
    # returns, the resource is gone or on an unstoppable timer. Azure has more
    # recovery windows than most clouds -- Key Vault soft-delete, storage
    # account blob soft-delete, SQL point-in-time restore -- so this flag is
    # for the genuinely final ones.
    irreversible: bool = False

    @property
    def summary(self) -> str:
        """``DELETE /subscriptions/.../disks/scratch``, for the dry run."""
        return f"{self.method.upper()} {self.path}"


PlanFn = Callable[[ScanContext, Finding], Iterator[Step]]
CLEANERS: dict[str, PlanFn] = {}


def cleaner(check: str) -> Callable[[PlanFn], PlanFn]:
    def decorator(fn: PlanFn) -> PlanFn:
        if check in CLEANERS:
            raise ValueError(f"duplicate cleaner for {check}")
        CLEANERS[check] = fn
        return fn

    return decorator


def backup_name(prefix: str) -> str:
    """A unique, self-describing name for a pre-delete snapshot.

    Azure resource names are validated per provider -- a snapshot allows 80
    characters of letters, digits, underscores, periods and hyphens -- so a
    timestamp is the only safe way to keep two runs from colliding.
    """
    return f"{prefix}-zombiescan-{_stamp()}"[:80].rstrip("-.")


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")


def snapshot_step(finding: Finding, disk_id: str, location: str, name: str = "") -> Step:
    """Snapshot one managed disk before something destroys it.

    An incremental snapshot bills only the blocks that differ from the disk's
    previous snapshot, so taking one before a delete is close to free where a
    disk has been snapshotted before and a full copy where it has not. Either
    way it is cheaper than the disk it replaces, which is what makes
    snapshot-then-delete the default rather than a flag.
    """
    snapshot = name or backup_name(finding.resource_id)
    group = finding.resource_group
    path = (
        f"/subscriptions/{finding.subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.Compute/snapshots/{snapshot}"
    )
    return Step(
        description=f"snapshot {finding.resource_id} to {snapshot} before deleting it",
        method="PUT",
        path=path,
        resource_type="Microsoft.Compute/snapshots",
        body={
            "location": location,
            "properties": {
                "creationData": {"createOption": "Copy", "sourceResourceId": disk_id},
                "incremental": True,
            },
        },
    )
