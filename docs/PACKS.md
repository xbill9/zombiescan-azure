# Writing a zombiescan pack

A **pack** is a unit of scan coverage: a set of checks, the cleaners that
remove what they find, the rates that price them, and the fetchers that refresh
those rates. The checks that ship with zombiescan are packs (`core`, `aks`)
and load through exactly the same path as one you install, so nothing here is a
special case reserved for built-ins.

```
$ zombiescan packs

  Pack   Version   Checks   Source
 ────────────────────────────────────
  aks    0.1.0     1        built-in
  core   0.1.0     21       built-in

pack API v1
```

## Before you write one

**A pack is code, and it runs with your Azure credentials.** Nothing
sandboxes it, and nothing verifies that its checks only read. `zombiescan scan`
promises to make read calls only; that promise is kept by the people who write
the checks, not by the machinery that runs them. Install packs on the same
judgement you would apply to any other dependency, and read a pack's checks
before you trust them against a production subscription.

If you are adding coverage to zombiescan itself rather than shipping your own
distribution, you do not need a pack at all — add a module to
`src/zombiescan/packs/core/`. It is picked up by existing.

## The shape

```
zombiescan-pack-acme/
  pyproject.toml
  src/zombiescan_pack_acme/
    __init__.py       registers the pack, then imports the rest
    rates.py          rate specs, if the pack prices anything new
    refresh.py        where those rates come from
    cleaners.py       how to remove what the checks find
    widgets.py        a check
```

Declare the entry point so zombiescan can find it:

```toml
[project.entry-points."zombiescan.packs"]
acme = "zombiescan_pack_acme"

[project.dependencies]
zombiescan = ">=0.1"
```

The entry point **name** is the pack name users will see and pass to
`--disable-pack`. The **value** is the module that registers it.

### `__init__.py`

```python
from zombiescan.packs import import_pack_modules, register_pack

register_pack(
    "acme",
    version="1.0.0",
    description="Waste checks for ACME's own services",
    homepage="https://github.com/acme/zombiescan-pack-acme",
)

from zombiescan_pack_acme import rates  # noqa: E402,F401

import_pack_modules(__name__, list(__path__))
```

`register_pack` must come first: everything registered afterwards is attributed
to the pack that is currently loading. Rates are imported explicitly before the
checks, because a check prices itself at import time is not a thing — but a
check module that referenced an unregistered rate key would fail on its first
run rather than at load, which is a worse place to find out.


## Writing a check

Either register a function:

```python
from collections.abc import Iterator

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "acme-idle-widget"
RESOURCE_TYPE = "Acme.Widgets/widgets"


@check(CHECK_NAME, "Widgets nobody is using", providers="Acme.Widgets")
def idle_widget(ctx: ScanContext) -> Iterator[Finding]:
    for widget in ctx.list(RESOURCE_TYPE):
        properties = helpers.properties(widget)
        if properties.get("state") != "Idle":
            continue
        arm_id = widget["id"]
        location = helpers.location_of(widget)
        group = widget.get("resourceGroup") or azure.resource_group_of(arm_id)
        region = azure.region_of(location)
        price, approximate = ctx.pricing.rate("acme.widget_month", region=region)
        yield Finding(
            check=CHECK_NAME,
            resource_id=widget["name"],
            resource_type="widget",
            subscription=ctx.subscription,
            resource_group=group,
            arm_id=arm_id,
            location=location,
            reason=f"Widget has been idle since {properties['idleSince']}",
            monthly_cost=price,
            remediation=helpers.az(
                f"az acme widget delete --name {helpers.arg(widget['name'])}",
                ctx.subscription,
                group,
            ),
            approximate_cost=approximate,
            details={"idle_since": properties["idleSince"]},
        )
```

…or, when the check really is one call and one filter, build it declaratively:

```python
from zombiescan import helpers
from zombiescan.building import simple_check

idle_widget = simple_check(
    "acme-idle-widget",
    "Widgets nobody is using",
    resource_type="Acme.Widgets/widgets",
    kind="widget",
    where=lambda w: helpers.properties(w).get("state") == "Idle",
    reason="Widget has been idle since {idleSince}",
    command="az acme widget delete --name {id}",
    rate="acme.widget_month",
)
```

`simple_check` reads `id`, `name`, `location` and `resourceGroup` off each
resource, adds the subscription and resource group to the command through
`helpers.az`, and takes the check's provider from the resource type. Pass
`graph="Resources | where ..."` to run a Resource Graph query instead of an ARM
list call; the rows it returns are used exactly as ARM resources would be, so
the query must project at least those four fields.

Use `simple_check` when the whole check fits it. Reach for a plain function as
soon as you need a second call, a sum over sub-resources, or a judgement a
predicate cannot express — most real checks do, and forcing them through the
builder makes them harder to read, not easier.

### Four rules that are not negotiable

- **Checks read. Never write.** ARM GETs and Resource Graph queries. A check
  that mutates anything is a bug.
- **A check runs once per subscription**, not once per region. There is no
  location scope to set: an ARM list call covers every resource group and
  region at once. Set each finding's `location` from the resource itself, and
  `global` where it genuinely has none.
- **Every finding carries its `resource_group` and its `arm_id`.** No `az`
  command works without the group, and the ARM id is the only identifier unique
  across a tenant — it is what your cleaner will act on. `resource_id` holds
  the bare name, because that is what a report shows.
- **Declare the providers you read** with `providers=`. This is load-bearing,
  not documentation: ARM answers a list call against an *unregistered* provider
  with HTTP 200 and an empty page, so a check whose provider is missing would
  find nothing and the subscription would read as clean. The engine refuses to
  run it instead. The same declaration generates `zombiescan providers` and the
  read-only role in `policy/`.

Where a provider only offers a per-parent list — Key Vault's keys and secrets,
a SQL server's databases — call `ctx.arm.list(f"{parent_id}/keys", TYPE)` for
each parent, and use `helpers.across_parents` if there are enough parents
that serial round trips would dominate the scan — it walks them in parallel
and lets one unreachable parent contribute nothing rather than failing the
check.

### Pinning an api-version

Azure has no "latest": `api-version` is a required query parameter. Add your
types to `azure.API_VERSIONS`, keyed by resource type — the lookup resolves by
longest prefix, so `Acme.Widgets/widgets/parts` inherits `Acme.Widgets/widgets`
unless it names its own.

**A pin that goes stale fails quietly.** A retired version produces
`InvalidResourceType`, which is 404-shaped, so `classify` reads it as `missing`
and the engine counts the pair as *unavailable* rather than raising. The check
stops running and the subscription looks that much cleaner. Verify a pin
against `az provider show -n <namespace>` and re-verify it when you touch the
check.

## Pricing

A rate is a key plus a description of how to read a section of the price table:

```python
from zombiescan.pricing.rates import RateSpec, register_rate

register_rate(RateSpec("acme.widget_month", "acme_widget_month", pack="acme"))
```

Every lookup returns `(usd_per_month, approximate)`. `approximate=True` means
the number is a stand-in — another region's rate, or a variant the table has
never heard of — and the report marks it, so never return it for a figure you
are confident in. Four shapes are built in:

| Shape | Section in the table | Spec |
| --- | --- | --- |
| Flat per-region | `{region: price}` | `RateSpec(key, section)` |
| Hourly per-region | `{region: price}` | `per_hour=True` |
| Keyed by variant | `{region: {variant: price}}` | `variants=True` |
| Global | one value, no region | `scope="global"` |

Regions are keyed as ARM spells them — `eastus`, not `East US`.
`azure.region_of` normalises both.

`default_variant` says what to price an unknown variant as. Leave it unset when
there is no honest answer: managed disk tiers span three orders of magnitude,
so `disk.tier_month` has no default and an unrecognised tier reports zero
marked approximate rather than the price of some other tier.

For anything these cannot express, register a resolver:

```python
from zombiescan.pricing.rates import register_resolver


def _tiered(table, units):
    rates = table.section("acme_tiers") or {}
    ...
    return price, approximate


register_resolver("acme.tiered_month", _tiered, pack="acme")
```

### Refreshing those rates

`python -m zombiescan.pricing.refresh` rebuilds the whole table from every
installed pack, so a pack that adds a rate must also say where it comes from,
or its prices are dropped on the next refresh. The test suite enforces this for
the built-in packs: every section of the table has exactly one fetcher.

```python
from zombiescan.pricing.refresh import RefreshContext, flat_by_region, price_fetcher


@price_fetcher("acme_widget_month", label="ACME widget prices", pack="acme")
def fetch_widgets(ctx: RefreshContext) -> dict[str, dict[str, float]]:
    rows = ctx.rows("serviceName eq 'ACME Widgets'", "unitOfMeasure eq '1/Month'")

    def is_capacity(row):
        return row["meterName"] == "Widget Capacity"

    return {"acme_widget_month": flat_by_region(rows, is_capacity)}
```

A fetcher returns `{section: data}` and may only write sections it declared.
`ctx.rows(*conditions)` queries the public Azure Retail Prices API — no
credentials — and adds `priceType eq 'Consumption'` for you.

Four traps, all of which fail silently. Every one is measured against the live
API in this repository's own fetchers; measure yours too rather than trusting a
price you remember:

- **`priceType eq 'Consumption'`.** The same meter is published as
  `Reservation` and `DevTestConsumption` at a fraction of the price.
  `ctx.rows` adds the clause; do not build a query that bypasses it.
- **`unit_price(rows)`, never the first row.** A tiered meter's rows come back
  in no guaranteed order and tier 0 is often a free allowance — Log Analytics
  ingestion is $0.00 up to 5 GB and $2.30 after. `unit_price` sorts by
  `tierMinimumUnits` and takes the first tier that charges. `by_region`,
  `flat_by_region` and `flat_global` group before pricing so they can.
- **Match the meter name, not just the SKU.** `P80 LRS Disk` is $3,604.11 a
  month; `P80 LRS Disk Mount` is $219.00, under the same `skuName`.
  `meter_is(row, "Disk")` is what separates them.
- **A meter published without an ARM region has no per-region entry.**
  `armRegionName` comes back as "Global", or as a billing geography spelled
  "Zone 1" — neither is an ARM region, and `by_region` drops both. Put those in
  a global section (`{"_value": price}`) with a `scope="global"` rate spec.

## Cleaning

A cleaner **plans**; it never executes. It yields the mutating requests that
would resolve a finding, and the runner in `zombiescan.clean` decides whether
to send them. `--apply` gates exactly one thing — whether a planned step is
sent to Azure — and must never change which steps get planned.

```python
from collections.abc import Iterator

from zombiescan.cleaners import Step, cleaner
from zombiescan.models import Finding, ScanContext


@cleaner("acme-idle-widget")
def clean_idle_widget(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    yield Step(
        description=f"back up widget {finding.resource_id}",
        method="POST",
        path=f"{finding.arm_id}/backup",
        resource_type="Acme.Widgets/widgets",
    )
    yield Step(
        description=f"delete widget {finding.resource_id}",
        method="DELETE",
        path=finding.arm_id,
        resource_type="Acme.Widgets/widgets",
        irreversible=True,
    )
```

A step is an HTTP verb against an ARM path, plus the resource type its
`api-version` is pinned from and an optional JSON `body`. Naming the request as
data rather than closing over it is what lets a dry run print exactly what it
would send: `DELETE /subscriptions/.../widgets/w-1` is something an operator
can check against the portal before agreeing to it.

- **Back up first where the API allows it**, and order the steps so the backup
  precedes the destruction. A failed step aborts the rest of that finding, so a
  failed backup can never be followed by the delete that assumed it.
- **Use `finding.arm_id`.** It is the whole path, already correct. Rebuilding
  it from the subscription, group and name is how a cleaner ends up addressing
  the wrong resource when a name is reused in two groups.
- **Mark `irreversible=True`** only where there is no recovery window at all.
  Azure gives several destructive calls one — a Key Vault key is held by
  mandatory soft-delete, a SQL database restores from point-in-time backups —
  and marking those irreversible trains the operator to ignore the warning. A
  wrong flag either way is a safety bug.
- **Read during planning if you need to.** Planning is allowed to make read
  calls, and sometimes must: the `empty-resource-group` cleaner re-lists the
  group and refuses if anything has appeared since the scan, because `az group
  delete` removes what it was never shown.
- **Refuse rather than guess.** If the safe action cannot be worked out from a
  list call, write no cleaner and pass `uncleanable="..."` to `@check`
  instead. The reason is shown to the operator verbatim, so write it for them:

```python
@check(
    CHECK_NAME,
    "Widgets nobody is using",
    providers="Acme.Widgets",
    uncleanable=(
        "a widget may still be referenced by a pipeline this tool cannot see, "
        "and deletion has no recovery window"
    ),
)
```

Every check needs either a cleaner or a reason. The built-in suite asserts it,
and it is worth asserting in yours.

### The generated command

`helpers.az(command, subscription, resource_group)` completes an `az` command:
it appends `--resource-group` and `--subscription`, and adds `--yes` **only**
where the command would otherwise prompt.

That last part is the Azure-specific trap. `gcloud` takes `--quiet` on
everything; `az` has no global equivalent, and passing `--yes` to a command
that does not take one is an error rather than a no-op —
`az network nic delete --yes` fails outright. `helpers.CONFIRMS` lists the
commands that accept it. Check yours with `az <command> --help` before adding
it there.

Wrap every interpolated resource name in `helpers.arg`. Azure's naming rules
make a shell break-out unreachable through most providers today, but a tool
whose whole proposition is handing someone a script to run should not depend on
a remote service's input validation for local shell safety — and resource
*group* names allow parentheses and periods.

## Compatibility

`register_pack` refuses a pack built against a different `api_version` rather
than half-loading it — a pack that imports cleanly but misprices findings is
worse than one that does not load at all. The current version is
`zombiescan.packs.PACK_API_VERSION` (**1**), and it is bumped when a change to
`ScanContext`, `Finding`, or the check/cleaner/rate registries stops an older
pack from working.

A pack that fails to import is reported and skipped, never fatal: a broken pack
of yours must not cost someone the twenty-one checks that would have worked.

The JSON report records every loaded pack and its version under `packs`
(`schema_version` 4). A finding means nothing without knowing which check
produced it, and a check means nothing without knowing which pack — two packs
may both ship an `idle-cluster` check and disagree about what idle means.

## Testing

Test against recorded ARM responses, not a live subscription. `zombiescan`'s
own suite runs offline with no credentials, and yours should:

```python
def test_flags_the_idle_widget(make_context):
    ctx, _ = make_context(
        {
            "Acme.Widgets/widgets": {
                "value": [
                    {
                        "id": "/subscriptions/s/resourceGroups/rg"
                        "/providers/Acme.Widgets/widgets/w-1",
                        "name": "w-1",
                        "location": "eastus",
                        "resourceGroup": "rg",
                        "properties": {"state": "Idle", "idleSince": "2026-01-02"},
                    },
                ]
            }
        }
    )
    assert [f.resource_id for f in idle_widget(ctx)] == ["w-1"]
```

Responses are keyed by **the tail of a request path**, longest match first, and
may be a callable of the call's arguments when one fake has to answer
differently per parent. That matters for sub-resources: Key Vault's vaults,
keys and secrets share a resource type, so they are keyed `/keys` and
`/secrets` rather than by the type. Resource Graph rows are keyed by a
substring of the query.

**Include a resource that is genuinely in use in every fixture.** Half of what
a check does is not reporting things, and a fixture containing only waste tests
none of it.

Pin your own price table in the fixture rather than loading the bundled one, so
that refreshing real prices cannot break an assertion.
