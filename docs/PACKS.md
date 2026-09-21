# Writing a zombiescan pack

A **pack** is a unit of scan coverage: a set of checks, the cleaners that
remove what they find, the rates that price them, and the fetchers that refresh
those rates. The checks that ship with zombiescan are packs (`core`, `gke`)
and load through exactly the same path as one you install, so nothing here is a
special case reserved for built-ins.

```
$ zombiescan packs

  Pack   Version   Checks   Source
 ────────────────────────────────────
  core   0.1.0     19       built-in
  gke    0.1.0     1        built-in

pack API v1
```

## Before you write one

**A pack is code, and it runs with your Google Cloud credentials.** Nothing
sandboxes it, and nothing verifies that its checks only read. `zombiescan scan`
promises to make list and get calls only; that promise is kept by the people who
write the checks, not by the machinery that runs them. Install packs on the same
judgement you would apply to any other dependency, and read a pack's checks
before you trust them against a production project.

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

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "acme-idle-widget"


@check(CHECK_NAME, "Widgets nobody is using", apis="compute")
def idle_widget(ctx: ScanContext) -> Iterator[Finding]:
    for scope, widget in gcp.aggregated(
        ctx.client("compute"), "widgets", "widgets", project=ctx.project
    ):
        if widget["status"] != "IDLE":
            continue
        location = gcp.location_from_scope(scope)
        price, approximate = ctx.pricing.rate("acme.widget_month", region=gcp.region_of(location))
        yield Finding(
            check=CHECK_NAME,
            resource_id=widget["name"],
            resource_type="widget",
            project=ctx.project,
            location=location,
            reason=f"Widget has been idle since {widget['idleSince']}",
            monthly_cost=price,
            remediation=helpers.gcloud(
                f"gcloud compute widgets delete {helpers.arg(widget['name'])}",
                ctx.project,
                location,
            ),
            approximate_cost=approximate,
            details={"idle_since": widget["idleSince"]},
        )
```

…or, when the check really is one call and one filter, build it declaratively:

```python
from zombiescan.building import simple_check

idle_widget = simple_check(
    "acme-idle-widget",
    "Widgets nobody is using",
    api="compute",
    path="widgets",
    aggregated_key="widgets",  # drop this for a plain list call
    id_key="name",
    resource_type="widget",
    where=lambda w: w["status"] == "IDLE",
    reason="Widget has been idle since {idleSince}",
    remediation="gcloud compute widgets delete {id} --zone={location} --project={project} --quiet",
    rate="acme.widget_month",
)
```

Use `simple_check` when the whole check fits it. Reach for a plain function as
soon as you need a second API call, a sum over sub-resources, or a judgement a
predicate cannot express — most real checks do, and forcing them through the
builder makes them harder to read, not easier.

### Four rules that are not negotiable

- **Checks read. Never write.** List and get. A check that mutates anything is
  a bug.
- **A check runs once per project**, not once per region. There is no location
  scope to set: `aggregatedList` and the `locations/-` wildcard each cover
  every location in one call. Set each finding's `location` from the resource
  itself — the aggregation scope, a `zone`/`region` field, or the resource
  path.
- **Never share a discovery client between threads.** Its `httplib2`
  connection is not safe to, and the interpreter segfaults rather than raising.
  If your check fans out, use `helpers.across_locations`, which hands each
  worker the client `Clients` built for that thread. Never build one in the
  calling thread and close over it.
- **Declare the APIs you call** with `apis=`. `zombiescan apis` and the
  read-only role in `policy/` are generated from it, and a check missing from
  the role reports nothing under it while looking like a clean project.

Two APIs — Cloud KMS and Artifact Registry — reject `locations/-` and have to
be walked location by location. `helpers.across_locations(ctx, api, fetch)`
does that in parallel; `fetch` is called as `fetch(client, location)`.

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

`default_variant` says what to price an unknown variant as. Leave it unset when
there is no honest answer: a versioned machine size that appears in the API
before the price table knows about it should report zero marked approximate,
not the price of a different machine.

For anything these cannot express, register a resolver:

```python
from zombiescan.pricing.rates import register_resolver


def _tiered(table, region, units):
    rates, approximate = table.lookup_section("acme_tiers", region)
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
from zombiescan.pricing.refresh import RefreshContext, price_fetcher, regions_of, unit_price


@price_fetcher("acme_widget_month", label="ACME widget prices", pack="acme")
def fetch_widgets(ctx: RefreshContext) -> dict[str, dict[str, float]]:
    rates = {}
    for sku in ctx.skus("ACME Widgets"):
        if sku["category"]["resourceGroup"] != "Widgets":
            continue
        price = unit_price(sku)
        for region in regions_of(sku):
            rates[region] = price
    return {"acme_widget_month": rates}
```

A fetcher returns `{section: data}` and may only write sections it declared.
`ctx.skus(display_name)` yields every on-demand SKU of one Cloud Billing
Catalog service, looked up by the display name rather than by its opaque id.

Two traps, both of which fail silently:

- **`unit_price(sku)`, never tier 0 directly.** Google fronts many SKUs with a
  free allowance priced at zero. Reading tier 0 of one records the rate as
  free, which prices every finding in your section at nothing and makes the
  scan look like an all-clear. `usd(sku, tier=n)` exists for reading a named
  tier deliberately, the way the Cloud DNS fetcher walks the zone tiers.
- **A SKU published against the region `global` has no per-region entry.**
  `regions_of` returns nothing for it. Put those in a global section
  (`{"_value": price}`) with a `scope="global"` rate spec.

## Cleaning

A cleaner **plans**; it never executes. It yields the mutating calls that would
resolve a finding, and the runner in `zombiescan.clean` decides whether to make
them. `--apply` gates exactly one thing — whether a planned step is sent to
Google Cloud
— and must never change which steps get planned.

```python
from collections.abc import Iterator

from zombiescan.cleaners import Step, cleaner
from zombiescan.models import Finding, ScanContext


@cleaner("acme-idle-widget")
def clean_idle_widget(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    yield Step(
        description=f"snapshot widget {finding.resource_id}",
        api="compute",
        operation="widgets.createSnapshot",
        params={
            "project": finding.project,
            "zone": finding.location,
            "widget": finding.resource_id,
        },
    )
    yield Step(
        description=f"delete widget {finding.resource_id}",
        api="compute",
        operation="widgets.delete",
        params={
            "project": finding.project,
            "zone": finding.location,
            "widget": finding.resource_id,
        },
        irreversible=True,
    )
```

`operation` is a dotted path into the discovery client: `"widgets.delete"` is
`compute.widgets().delete`, and `"projects.secrets.delete"` is
`secretmanager.projects().secrets().delete`. Naming the call as data rather
than closing over it is what lets a dry run print the exact request it would
send.

- **Back up first where the API allows it**, and order the steps so the backup
  precedes the destruction. A failed step aborts the rest of that finding, so a
  failed snapshot can never be followed by the delete that assumed it.
- **Put the whole location in the params.** There is no implicit region: a
  step carries `project` and whichever of `zone`, `region` or a full resource
  `name` the call takes, read off the finding.
- **Mark `irreversible=True`** only where there is no recovery window at all.
  Google gives several destructive calls one — a destroyed KMS key version is
  held for 24 hours — and marking those irreversible trains the operator to
  ignore the warning. A wrong flag either way is a safety bug.
- **Refuse rather than guess.** If the safe action cannot be worked out from a
  list call, write no cleaner and pass `uncleanable="..."` to `@check`
  instead. The reason is shown to the operator verbatim, so write it for them:

```python
@check(
    CHECK_NAME,
    "Widgets nobody is using",
    uncleanable=(
        "a widget may still be referenced by a pipeline this tool cannot see, "
        "and deletion has no recovery window"
    ),
)
```

Every check needs either a cleaner or a reason. The built-in suite asserts it,
and it is worth asserting in yours.

## Compatibility

`register_pack` refuses a pack built against a different `api_version` rather
than half-loading it — a pack that imports cleanly but misprices findings is
worse than one that does not load at all. The current version is
`zombiescan.packs.PACK_API_VERSION` (**1**), and it is bumped when a change to
`ScanContext`, `Finding`, or the check/cleaner/rate registries stops an older
pack from working.

A pack that fails to import is reported and skipped, never fatal: a broken pack
of yours must not cost someone the nineteen checks that would have worked.

The JSON report records every loaded pack and its version under `packs`
(`schema_version` 3). A finding means nothing without knowing which
check produced it, and a check means nothing without knowing which pack — two
packs may both ship an `idle-cluster` check and disagree about what idle means.

## Testing

Test against recorded API responses, not a live account. `zombiescan`'s own
suite runs offline with no credentials, and yours should:

```python
def test_flags_the_idle_widget(make_context):
    ctx, _ = make_context(
        {
            "widgets.aggregatedList": {
                "items": {
                    "zones/us-central1-a": {"widgets": [{"name": "w-1", "status": "IDLE"}]},
                    "zones/us-east1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}},
                }
            }
        }
    )
    assert [f.resource_id for f in idle_widget(ctx)] == ["w-1"]
```

Responses are keyed by the dotted `collection.method` path the check calls, and
may be a callable of the call's kwargs when one fake has to answer differently
per argument. Include a scope holding only a `warning` in every aggregated
fixture: that is how Compute reports an empty zone, and a check that does not
skip it crashes on the first real scan.

Pin your own price table in the fixture rather than loading the bundled one, so
that refreshing real prices cannot break an assertion.
