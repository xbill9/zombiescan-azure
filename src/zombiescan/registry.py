"""Check registry.

Every check registers itself with ``@check(...)``. Discovering packs
(``zombiescan.packs.discover``) imports their check modules, which populates
this registry as a side effect.

There is no location scope on a check. One Resource Graph query answers for
every region and every resource group a subscription has, so a check runs once
per subscription and reads each finding's location off the resource it found.

A check declares the resource provider namespaces it reads. That declaration
is load-bearing rather than documentation: ARM answers a list call against an
unregistered provider with an empty page and HTTP 200, so the engine refuses
to run a check whose provider is missing instead of letting it report a clean
subscription. The read-only role in ``policy/`` and ``zombiescan providers``
are generated from the same declaration.
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
    # Which Azure resource provider namespaces this check reads, e.g.
    # ("Microsoft.Compute",). The first one is the check's primary provider:
    # if it is not registered on a subscription, the check is not run there.
    providers: tuple[str, ...] = ()
    # Why this finding cannot be cleaned automatically, if it cannot. A check
    # must have either a cleaner or a reason here: "refuse rather than guess"
    # only works if the refusal explains itself.
    uncleanable: str | None = None

    @property
    def primary_provider(self) -> str:
        """The provider whose absence means this check has nothing to find."""
        return self.providers[0] if self.providers else ""


CHECKS: dict[str, CheckSpec] = {}


def check(
    name: str,
    title: str,
    providers: tuple[str, ...] | str = (),
    uncleanable: str | None = None,
) -> Callable[[CheckFn], CheckFn]:
    """Register a check under ``name``.

    Checks must make read calls only -- ARM GETs and Resource Graph queries. A
    check that mutates anything is a bug, not a feature request.

    ``uncleanable`` states why this finding cannot be removed automatically.
    Set it instead of writing a cleaner when the safe action genuinely cannot
    be worked out from a list call -- it is reported to the operator verbatim.
    """

    if isinstance(providers, str):
        providers = (providers,)

    def decorator(fn: CheckFn) -> CheckFn:
        if name in CHECKS:
            raise ValueError(f"duplicate check name: {name}")
        CHECKS[name] = CheckSpec(
            name=name,
            title=title,
            fn=fn,
            pack=packs.current_pack(),
            providers=tuple(providers),
            uncleanable=uncleanable,
        )
        return fn

    return decorator
