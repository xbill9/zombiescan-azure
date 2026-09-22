"""Building checks declaratively.

Most of what a check does is the same every time: run one query, drop the
resources that are in use, and turn the rest into findings. The part that is
never the same is the judgement -- what makes this resource waste, how to say
so, and what it costs.

``simple_check`` takes the first part as data and leaves the second to you.
Use it when a check really is "list these, keep the ones matching this, price
them at this rate". Reach for a plain ``@check`` function the moment the check
needs to cross-reference a second query, sum over sub-resources, or make a
judgement a predicate cannot express -- most of the checks in this repository
do, and forcing them through here would make them harder to read, not easier.

    from zombiescan.building import simple_check

    unused_widget = simple_check(
        "unused-widget",
        "Widgets nobody is using",
        resource_type="Microsoft.Acme/widgets",
        kind="widget",
        where=lambda w: helpers.properties(w).get("state") == "Idle",
        reason=lambda w, ctx: f"Widget has been idle since {w['idleSince']}",
        command="az acme widget delete --name {id}",
        rate="widget.month",
    )
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import CheckFn, check

# A value that may be read straight off the resource by key, or computed.
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
    resource_type: str,
    kind: str,
    reason: str | Callable[[dict[str, Any], ScanContext], str],
    command: str | Callable[[dict[str, Any], ScanContext], str],
    graph: str | None = None,
    id_key: Getter = "name",
    rate: str | None = None,
    quantity: Getter = None,
    variant: Getter = None,
    where: Callable[[dict[str, Any]], bool] | None = None,
    details: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    providers: tuple[str, ...] | str = (),
    uncleanable: str | None = None,
) -> CheckFn:
    """Register a check that is one query, one filter and one rate.

    By default the check makes an ARM list call for ``resource_type``, which
    covers every resource group in the subscription. Pass ``graph`` to use a
    Resource Graph query instead, when the filtering is better done in KQL
    than in Python -- the rows it returns are used exactly as an ARM resource
    would be, so a query must project at least ``id``, ``name``, ``location``
    and ``resourceGroup``.

    ``rate`` is a key registered with ``zombiescan.pricing.rates``; the
    finding's cost is its price multiplied by ``quantity`` (1 by default, so a
    per-resource rate needs no quantity at all). With no ``rate`` the finding
    costs nothing, which is the right answer for the hygiene checks.

    ``reason`` and ``command`` are either format strings -- ``{id}``,
    ``{arm_id}``, ``{location}``, ``{region}``, ``{group}`` and
    ``{subscription}`` are substituted, along with every key of the resource
    that those names do not already claim -- or callables taking
    ``(resource, ctx)``. ``command`` is completed into a full ``az`` command
    by ``helpers.az``, which adds the resource group, the subscription and
    ``--yes`` where the command needs it.
    """

    def build_finding(ctx: ScanContext, item: dict[str, Any]) -> Finding:
        resource_id = _read(item, id_key)
        arm_id = item.get("id") or ""
        group = item.get("resourceGroup") or azure.resource_group_of(arm_id)
        location = helpers.location_of(item)
        region = azure.region_of(location)

        approximate = False
        monthly_cost = 0.0
        if rate is not None:
            price, approximate = ctx.pricing.rate(
                rate,
                region=region,
                **({"variant": _read(item, variant)} if variant is not None else {}),
            )
            monthly_cost = float(_read(item, quantity, 1.0) or 0.0) * price

        def render(template: str | Callable[[dict[str, Any], ScanContext], str]) -> str:
            if callable(template):
                return template(item, ctx)
            # The named substitutions are applied *over* the resource's own
            # keys rather than alongside them. Every ARM resource carries an
            # `id` -- its full path -- so passing both would collide on the
            # one name every template uses, and `{id}` means the short name
            # here. `{arm_id}` is the full path for a template that wants it.
            return template.format(
                **{
                    **item,
                    "id": resource_id,
                    "arm_id": arm_id,
                    "location": location,
                    "region": region,
                    "group": group,
                    "subscription": ctx.subscription,
                }
            )

        return Finding(
            check=name,
            resource_id=resource_id,
            resource_type=kind,
            subscription=ctx.subscription,
            resource_group=group,
            arm_id=arm_id,
            location=location,
            reason=render(reason),
            monthly_cost=monthly_cost,
            remediation=helpers.az(render(command), ctx.subscription, group),
            approximate_cost=approximate,
            details=dict(details(item)) if details else {},
        )

    @check(
        name,
        title,
        providers=providers or azure.namespace_of(resource_type),
        uncleanable=uncleanable,
    )
    def run(ctx: ScanContext) -> Iterator[Finding]:
        items = ctx.graph(graph) if graph is not None else ctx.list(resource_type)
        for item in items:
            if where is None or where(item):
                yield build_finding(ctx, item)

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
