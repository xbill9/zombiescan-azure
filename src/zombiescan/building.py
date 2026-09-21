"""Building checks declaratively.

Most of what a check does is the same every time: page through one list call,
drop the items that are in use, and turn the rest into findings. The part that
is never the same is the judgement -- what makes this resource waste, how to
say so, and what it costs.

``simple_check`` takes the first part as data and leaves the second to you.
Use it when a check really is "list these, keep the ones matching this, price
them at this rate". Reach for a plain ``@check`` function the moment the check
needs to cross-reference a second API call, sum over sub-resources, or make a
judgement a predicate cannot express -- most of the checks in this repository
do, and forcing them through here would make them harder to read, not easier.

    from zombiescan.building import simple_check

    unused_widget = simple_check(
        "unused-widget",
        "Widgets nobody is using",
        api="compute",
        path="widgets",
        aggregated_key="widgets",
        id_key="name",
        resource_type="widget",
        where=lambda w: w.get("status") == "IDLE",
        reason=lambda w, ctx: f"Widget has been idle since {w['idleSince']}",
        remediation="gcloud compute widgets delete {id} --project={project} --quiet",
        rate="widget.month",
    )
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any

from zombiescan import gcp
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import CheckFn, check

# A value that may be read straight off the API item by key, or computed.
Getter = str | Callable[[dict[str, Any]], Any] | None


def _read(item: dict[str, Any], getter: Getter, default: Any = None) -> Any:
    if getter is None:
        return default
    if callable(getter):
        return getter(item)
    return item.get(getter, default)


def simple_check(
    name: str,
    title: str,
    *,
    api: str,
    path: str,
    resource_type: str,
    id_key: Getter,
    reason: str | Callable[[dict[str, Any], ScanContext], str],
    remediation: str | Callable[[dict[str, Any], ScanContext], str],
    method: str = "list",
    result_key: str = "items",
    aggregated_key: str | None = None,
    location: Getter = None,
    rate: str | None = None,
    quantity: Getter = None,
    variant: Getter = None,
    where: Callable[[dict[str, Any]], bool] | None = None,
    details: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    params: Callable[[ScanContext], dict[str, Any]] | dict[str, Any] | None = None,
    apis: tuple[str, ...] | str = (),
    uncleanable: str | None = None,
) -> CheckFn:
    """Register a check that is one list call, one filter and one rate.

    Pass ``aggregated_key`` to use Compute Engine's ``aggregatedList``: the
    call then covers every zone and region at once and each finding's location
    comes from the scope the item was returned under. Otherwise the check makes
    a plain ``list`` call and reads the location from ``location``.

    ``rate`` is a key registered with ``zombiescan.pricing.rates``; the
    finding's cost is its price multiplied by ``quantity`` (1 by default, so a
    per-resource rate needs no quantity at all). With no ``rate`` the finding
    costs nothing, which is the right answer for the hygiene checks.

    ``reason`` and ``remediation`` are either format strings -- ``{id}``,
    ``{location}``, ``{region}`` and ``{project}`` are substituted, along with
    every key of the API item -- or callables taking ``(item, ctx)``.
    """

    def build_finding(ctx: ScanContext, item: dict[str, Any], where_: str) -> Finding:
        resource_id = _read(item, id_key)
        approximate = False
        monthly_cost = 0.0
        if rate is not None:
            price, approximate = ctx.pricing.rate(
                rate,
                region=gcp.region_of(where_),
                **({"variant": _read(item, variant)} if variant is not None else {}),
            )
            monthly_cost = float(_read(item, quantity, 1.0) or 0.0) * price

        def render(template: str | Callable[[dict[str, Any], ScanContext], str]) -> str:
            if callable(template):
                return template(item, ctx)
            return template.format(
                id=resource_id,
                location=where_,
                region=gcp.region_of(where_),
                project=ctx.project,
                **item,
            )

        return Finding(
            check=name,
            resource_id=resource_id,
            resource_type=resource_type,
            project=ctx.project,
            location=where_,
            reason=render(reason),
            monthly_cost=monthly_cost,
            remediation=render(remediation),
            approximate_cost=approximate,
            details=dict(details(item)) if details else {},
        )

    def _params(ctx: ScanContext) -> dict[str, Any]:
        if callable(params):
            return params(ctx)
        return dict(params or {})

    @check(name, title, apis=apis or api, uncleanable=uncleanable)
    def run(ctx: ScanContext) -> Iterator[Finding]:
        client = ctx.client(api)
        if aggregated_key is not None:
            pairs = gcp.aggregated(client, path, aggregated_key, **_params(ctx))
            items = ((gcp.location_from_scope(scope), item) for scope, item in pairs)
        else:
            items = (
                (_read(item, location, gcp.GLOBAL) or gcp.GLOBAL, item)
                for item in gcp.paginate(
                    client, path, method=method, key=result_key, **_params(ctx)
                )
            )
        for where_, item in items:
            if where is None or where(item):
                yield build_finding(ctx, item, where_)

    # Tests and cleaners reach for the finding builder directly, the same way
    # they do with a hand-written check.
    run.build_finding = build_finding  # type: ignore[attr-defined]
    # Attribute the check to the module that declared it rather than to this
    # one. Everything that asks a check where it came from -- tracebacks, and
    # the MCP server reading the module docstring to explain a finding -- would
    # otherwise be told building.py, which explains the wrong thing.
    caller = inspect.currentframe()
    if caller is not None and caller.f_back is not None:
        run.__module__ = caller.f_back.f_globals.get("__name__", run.__module__)
    return run
