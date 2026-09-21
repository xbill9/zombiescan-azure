# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`zombiescan` — an open-source CLI that scans Google Cloud projects for
resources nobody is using (unattached Persistent Disks, reserved static IPs
attached to nothing, idle GKE clusters, orphaned snapshots), prices them, and
reports the monthly waste total. Ships with a Claude Code plugin (skill + MCP
server) wrapping the same engine.

Check catalog, architecture, and build order: @PLAN.md

## Scope: local only

There is **no hosted component**. No web frontend, no API, no Cloud Function,
no cross-project service account, no publish endpoint. The scan runs on the
local machine against the local Google Cloud credentials and writes to the
terminal and local files.

Do not propose a hosted or deployed piece unless asked.

## Safety: scanning and cleaning are separate

`zombiescan scan` is **read-only**: list and get calls only. Never add a
mutating call to a check. A check that changes anything is a bug.

This is kept by convention, not enforced: nothing inspects a check's API calls
before running them. Since packs can be installed from PyPI and run with the
local credentials, do not claim the read-only guarantee holds for third-party
packs — it holds for the packs in this repository because they are reviewed.

`zombiescan clean` does delete things, and the guarantees around it must hold:

- **Dry run is the default.** `--apply` gates exactly one thing: whether a
  planned step is sent to Google Cloud. It must never change which steps get
  planned, or the preview stops being worth anything.
- **Planning is read-only.** Cleaners may make read calls to build a plan
  (fetching an instance's disks to clear `autoDelete`, reading a router's NAT
  list) but must only ever *yield* mutations as `Step` objects for the runner.
- **Back up first where the API allows it**, and order the steps so the backup
  precedes the destruction. A failed step aborts the rest of that finding, so a
  failed snapshot can never be followed by the delete that assumed it.
- **Mark `irreversible=True`** only where there is no recovery window at all. A
  destroyed KMS key version is held for 24 hours and is therefore *not* marked;
  a released static IP is gone and is. A wrong flag is a safety bug.
- **Refuse rather than guess.** No cleaner for a check means the finding is
  reported as unsupported with a reason — pass `uncleanable="..."` to
  `@check` — never approximated. A check with neither a cleaner nor a reason
  fails `tests/test_clean.py`.
- Never clean on the basis of a failed scan, and never clean using a `--from`
  report produced by different credentials.

## Credentials

Authentication is Application Default Credentials: `google.auth.default()`,
set up once with `gcloud auth application-default login`. No key files, no
static secrets in the repo.

**`gcp.make_thread_safe` wraps the credentials and must stay.** Every request
goes through `before_request`, which refreshes when the token is missing — so
at the start of a scan every worker thread calls `refresh` at the same moment.
That signs through OpenSSL via cffi, and running it concurrently on one
credentials object corrupts the heap: the process dies with SIGSEGV or a glibc
abort rather than an exception. The wrapper fetches the first token
single-threaded and serialises later refreshes behind a lock.

**Discovery clients are per-thread, and must stay that way.** The `httplib2`
connection under a client is not safe to share, and the same crash follows. The
`Clients` class holds them in thread-local storage and builds from the
discovery documents bundled with `google-api-python-client`, which costs
milliseconds and no network.

The corollary a check author has to remember: **never build a client in one
thread and close over it in another.** `helpers.across_locations` passes each
worker its own client for exactly this reason.

## Commands

```
uv sync                                    # install deps
uv run zombiescan scan --all-projects      # run the CLI
uv run zombiescan apis                     # which APIs the checks need enabled
uv run pytest                              # tests (fixtures, offline)
ZOMBIESCAN_LIVE=1 uv run pytest -m live    # opt-in live smoke test, real project
uv run zombiescan-mcp                      # the MCP server, on stdio
uv run ruff format . && uv run ruff check --fix .
```

## Locations, not regions

A check runs **once per project**, not once per region. Two facts about
Google's APIs make that correct rather than a shortcut:

- Compute Engine's `aggregatedList` returns every zone and region in one call.
- Most other APIs accept `locations/-`, a wildcard meaning every location.

So there is no location scope on `@check`. Each finding sets its own
`location` — a zone, a region, or `global` — from the aggregation scope, the
resource's own field, or its resource path. `--location` filters findings
afterwards, and naming a region keeps its zones.

Cloud KMS and Artifact Registry reject `locations/-`. Those two enumerate
locations with `helpers.across_locations`, which walks them in parallel and
hands each worker its own client.

## Packs

Checks live in packs under `src/zombiescan/packs/<pack>/`, discovered by
existing rather than listed in an import block. A pack owns its checks, its
cleaners (`cleaners.py`), its price rates (`rates.py`) and the fetchers that
refresh them (`refresh.py`). `core` and `gke` are built in and load through the
same entry-point-equivalent path as an installed pack, so breaking the seam
breaks every check and the suite says so.

Prices are looked up by key — `ctx.pricing.rate("disk.gb_month", region=...)` —
against specs registered in `zombiescan.pricing.rates`. Do not add a method to
`PriceTable` for a new rate; register a `RateSpec`, or a resolver if its shape
needs one. Every table section must have exactly one `@price_fetcher`, which
`tests/test_packs.py` enforces.

`docs/PACKS.md` is the pack-author guide. Keep it current — it is the contract
third-party packs are written against, along with `PACK_API_VERSION`.

Every check declares the APIs it calls via `apis=`. `zombiescan apis` and the
read-only role in `policy/` are both generated from that, and the suite fails
if a check names an API the role does not cover.

## Pricing

`python -m zombiescan.pricing.refresh` rebuilds `table.json` from the Cloud
Billing Catalog API. Two traps, both of which fail silently:

**Read the first tier that charges, not tier 0.** Google fronts many SKUs with
a free allowance priced at zero — the first 30 GB of standard Persistent Disk,
the first 0.5 GB of Artifact Registry, the first six secret versions. Reading
tier 0 records the rate as free, which prices every finding in that section at
nothing and makes the scan report a clean project. `unit_price()` is the
correct reader; `usd(sku, tier=n)` is only for reading a named tier
deliberately, the way the Cloud DNS fetcher walks the zone tiers.

**A SKU published against the region `global` has no per-region entry.** Cloud
NAT addresses, Artifact Registry storage and log retention are all like this;
they go in a global section with a `scope="global"` rate spec, not into an
empty per-region one.

`main` refuses to write a table that loses or empties a section the previous
one had, because an empty section is a failed fetch rather than a price of
zero.

This file is run twice by `python -m zombiescan.pricing.refresh` — once as
`__main__` with a `FETCHERS` list of its own, once as the canonical module that
packs register into. The `__main__` block hands over to the canonical `main()`
for that reason; calling `main()` directly there rebuilds the table from core's
sections alone and writes zero prices for every pack.

## The plugin and the MCP server

`plugin/` is the Claude Code plugin: two slash commands, a skill, and
`.mcp.json` pointing at the `zombiescan-mcp` console script. The server itself
is `src/zombiescan/mcp_server.py`.

**Every tool on it is read-only, and it must stay that way.** `plan_cleanup`
may call `clean.plan_for`, which only builds `Step` objects;
`clean.apply_outcome` sends them to Google Cloud and must never be reachable
from the server. `tests/test_mcp_server.py` asserts the module names no
`clean.*` attribute beyond `plan_for` and `UNSUPPORTED`, so a tool that applies
a plan fails the suite.

Tools return computed figures — totals, counts, breakdowns, cheapest and
costliest — and echo the filter they applied. Do not add a tool that returns
rows for the caller to add up.

The protocol is JSON-RPC over stdio, standard library only. Do not add an MCP
SDK dependency for about a hundred lines of framing.

## Testing

Check logic is tested against committed JSON fixtures of real API responses, so
the suite runs offline with no credentials. One end-to-end smoke test hits a
real project and is gated behind `ZOMBIESCAN_LIVE=1`.

When adding a check, add its fixture and test in the same change —
`tests/test_packs.py` fails if a check has no test file of its own.

Fixtures are keyed the way the discovery client returns them: `{"items": ...}`
for a list, `{"items": {"zones/...": {...}}}` for an aggregated one. Include a
scope holding only a `warning` in any aggregated fixture — that is how Compute
reports an empty zone, and a check that does not skip it crashes.

## Generated commands

Every remediation string is a `gcloud` command that must carry `--project` and
`--quiet`, and must wrap interpolated resource ids in `helpers.arg`.
`tests/test_report.py` checks all three at the source. Validate the leaf
command's syntax with `gcloud <command> --help` before writing it; the flags
differ more than they look (`compute snapshots delete` takes no location,
`compute addresses delete` takes `--global` or `--region`, `filestore instances
delete` takes `--location`).
