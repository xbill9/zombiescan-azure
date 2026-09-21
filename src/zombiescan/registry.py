"""Check registry.

Every check registers itself with ``@check(...)``. Discovering packs
(``zombiescan.packs.discover``) imports their check modules, which populates
this registry as a side effect.

There is no location scope on a check. Compute Engine's ``aggregatedList`` and
the ``locations/-`` wildcard both answer for every location in one call, so a
check runs once per project and reads each finding's location off the resource
it found. A check that genuinely needs to walk locations one at a time loops
over them itself, using the project and clients it is handed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from zombiescan import packs
from zombiescan.models import Finding, ScanContext

CheckFn = Callable[[ScanContext], Iterator[Finding]]


@dataclass(frozen=True)
class CheckSpec:
    name: str
    title: str
    fn: CheckFn
    # Which pack registered this check, for reporting and for --disable-pack.
    pack: str = "core"
    # Which Google Cloud APIs this check calls. Used to generate the read-only
    # role in policy/ and to tell an operator which API to enable -- a project
    # with the API switched off reports nothing, and should say why.
    apis: tuple[str, ...] = ()
    # Why this finding cannot be cleaned automatically, if it cannot. A check
    # must have either a cleaner or a reason here: "refuse rather than guess"
    # only works if the refusal explains itself.
    uncleanable: str | None = None


CHECKS: dict[str, CheckSpec] = {}


def check(
    name: str,
    title: str,
    apis: tuple[str, ...] | str = (),
    uncleanable: str | None = None,
) -> Callable[[CheckFn], CheckFn]:
    """Register a check under ``name``.

    Checks must make list/get calls only. A check that mutates anything is a
    bug, not a feature request.

    ``uncleanable`` states why this finding cannot be removed automatically.
    Set it instead of writing a cleaner when the safe action genuinely cannot
    be worked out from a list call -- it is reported to the operator verbatim.
    """

    if isinstance(apis, str):
        apis = (apis,)

    def decorator(fn: CheckFn) -> CheckFn:
        if name in CHECKS:
            raise ValueError(f"duplicate check name: {name}")
        CHECKS[name] = CheckSpec(
            name=name,
            title=title,
            fn=fn,
            pack=packs.current_pack(),
            apis=tuple(apis),
            uncleanable=uncleanable,
        )
        return fn

    return decorator
