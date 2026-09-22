# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this is

`zombiescan` — an open-source CLI that scans Azure subscriptions for resources
nobody is using (unattached managed disks, public IPs attached to nothing, idle
NAT gateways, App Service plans hosting no apps, orphaned snapshots), prices
them, and reports the monthly waste total. Ships with a Claude Code plugin
(skill + MCP server) wrapping the same engine.

Check catalog, architecture, and build order: @PLAN.md

## Scope: local only

There is **no hosted component**. No web frontend, no API, no Function App, no
cross-subscription service principal, no publish endpoint. The scan runs on the
local machine against the local Azure CLI credentials and writes to the
terminal and local files.

Do not propose a hosted or deployed piece unless asked.

## Safety: scanning and cleaning are separate

`zombiescan scan` is **read-only**: ARM GETs and Resource Graph queries only.
Never add a mutating call to a check. A check that changes anything is a bug.

This is kept by convention, not enforced: nothing inspects a check's requests
before running them. Since packs can be installed from PyPI and run with the
local credentials, do not claim the read-only guarantee holds for third-party
packs — it holds for the packs in this repository because they are reviewed.

`zombiescan clean` does delete things, and the guarantees around it must hold:

- **Dry run is the default.** `--apply` gates exactly one thing: whether a
  planned step is sent to Azure. It must never change which steps get planned,
  or the preview stops being worth anything.
- **Planning is read-only.** Cleaners may make read calls to build a plan
  (re-listing a resource group to confirm it is still empty, reading a vault's
  soft-delete setting) but must only ever *yield* mutations as `Step` objects
  for the runner.
- **Back up first where the API allows it**, and order the steps so the backup
  precedes the destruction. A failed step aborts the rest of that finding, so a
  failed snapshot can never be followed by the delete that assumed it.
- **Mark `irreversible=True`** only where there is no recovery window at all.
  Azure has more recovery windows than most clouds and the flag must reflect
  the real ones: a Key Vault key is held by mandatory soft-delete and is
  therefore *not* marked; a SQL database restores from point-in-time backups
  and is *not* marked; a released public IP address is gone and **is**. A wrong
  flag is a safety bug.
- **Refuse rather than guess.** No cleaner for a check means the finding is
  reported as unsupported with a reason — pass `uncleanable="..."` to
  `@check` — never approximated. A check with neither a cleaner nor a reason
  fails `tests/test_clean.py`.
- **`az group delete` is the one command that removes things it was not
  shown.** The `empty-resource-group` cleaner re-lists the group during
  planning and refuses if anything has appeared since the scan. Never remove
  that re-check.
- Never clean on the basis of a failed scan, and never clean using a `--from`
  report produced by different credentials.

## Credentials, and one token per tenant

Authentication is the Azure CLI: `az login`, once. `azure.Credential` shells
out to `az account get-access-token` and reuses the result. No key files, no
client secrets in the repo, no `azure-identity` dependency.

**An ARM token is issued for exactly one tenant**, so `Credential` holds one
per tenant rather than one per scan. This is not an enterprise edge case:
Microsoft lets a single email address be both a work or school account and a
personal Microsoft account, so one person having two tenants is ordinary. A
scanner holding one token would sweep whichever tenant happened to be current
and report the other's waste as absent.

Three pieces make that work, and none of them should be collapsed:

- **`azure.known_subscriptions()` reads `az account list --all`**, not ARM's
  `/subscriptions`. ARM cannot see past its token's tenant; `az` tracks every
  identity that has signed in. `--all-subscriptions` adds `--refresh` to pick
  up subscriptions created since the last login.
- **`Arm.token_source` keys the token cache by tenant**, mapped from the
  subscription. A subscription `az` has never seen is keyed on its own instead
  of borrowing the default tenant's token, which would be refused.
- **Each request reads its subscription out of its own ARM path** and picks
  the token from that, so nothing above the client layer threads a tenant
  through. Resource Graph is the one exception — its path names no
  subscription, they go in the body — and it passes `subscription=` explicitly.
  A new call whose path has no subscription must do the same.

**Every token is fetched before any worker thread exists**, by `Arm.prepare`,
which `engine.scan` calls before it creates the pool. Every request asks for a
token, so without that the start of a scan would have every worker shelling out
to `az` at the same moment. Concurrent `az account get-access-token` processes
contend on the MSAL token cache in `~/.azure` and can leave it corrupt, which
costs the operator an `az login` rather than a retry. The lock still guards
refreshes, so an expiry mid-scan refreshes exactly once.

The token response names the tenant it was issued for, so the first one —
fetched with no subscription named, to fail fast when nobody is signed in — is
filed under its own tenant too rather than fetched a second time.

There is no per-thread client to manage: ARM is one REST surface, each request
opens its own connection, and the only shared state is those tokens.

## The failure mode everything is built around

**ARM answers a list call against an unregistered resource provider with HTTP
200 and an empty page.** A subscription that has never used App Service returns
`{"value": []}` from `/providers/Microsoft.Web/serverfarms` — no error, no
signal. A check that simply ran would find nothing, and the scan would report a
clean subscription. That is a false all-clear, the worst output this tool can
produce.

So every check declares the providers it reads, `Arm.registered_providers`
reads each subscription's registrations once, and the engine refuses to run a
check whose provider is missing. **Never remove that pre-check on the grounds
that the call "works".**

The same `providers=` declaration generates `zombiescan providers` and the
read-only role in `policy/`, and the suite fails if a check names a provider
the role does not cover.

## API versions are pinned, and a stale pin fails quietly

Azure has no "latest": `api-version` is a required query parameter. A version
that has been retired produces `InvalidResourceType`, which is 404-shaped —
`classify` reads it as `missing` and the engine counts it as *unavailable*. The
check stops running and the subscription looks that much cleaner.

`azure.API_VERSIONS` is keyed by resource type and resolved by longest prefix,
so a sub-type inherits its parent's version. `tests/test_live.py` verifies
every pin against what ARM currently accepts; nothing offline can.

## Subscriptions and resource groups, not regions

A check runs **once per subscription**, not once per region. Two facts about
ARM make that correct rather than a shortcut:

- A list call at `/subscriptions/<id>/providers/<ns>/<type>` returns every
  resource of that type in every resource group and every region.
- Azure Resource Graph answers a cross-type join in one KQL query, so a check
  needing "which NICs have a VM" does not pull two inventories down.

So there is no location scope on `@check`. Each finding sets its own `location`
from the resource, and `--location` filters findings afterwards; a `global`
finding is always kept, because a DNS zone has no region to match.

**Every finding must carry its `resource_group` and its `arm_id`.** No `az`
command works without the group, and the ARM id is the only identifier unique
across a tenant — it is what cleaners act on. `resource_id` holds the bare name
because that is what a report should show.

## Packs

Checks live in packs under `src/zombiescan/packs/<pack>/`, discovered by
existing rather than listed in an import block. A pack owns its checks, its
cleaners (`cleaners.py`), its price rates (`rates.py`) and the fetchers that
refresh them (`refresh.py`). `core` and `aks` are built in and load through the
same entry-point-equivalent path as an installed pack, so breaking the seam
breaks every check and the suite says so.

Prices are looked up by key — `ctx.pricing.rate("disk.tier_month", region=...,
variant="P10 LRS")` — against specs registered in `zombiescan.pricing.rates`.
Do not add a method to `PriceTable` for a new rate; register a `RateSpec`, or a
resolver if its shape needs one. Every table section must have exactly one
`@price_fetcher`, which `tests/test_packs.py` enforces.

`docs/PACKS.md` is the pack-author guide. Keep it current — it is the contract
third-party packs are written against, along with `PACK_API_VERSION`.

## Pricing

`python -m zombiescan.pricing.refresh` rebuilds `table.json` from the Azure
Retail Prices API, which is public: no credentials, no subscription.

Four traps, all of which fail silently. Every one was measured against the live
API rather than recalled; re-measure before changing any of them.

**Filter on `priceType eq 'Consumption'`.** The same meter is published as
`Reservation` and `DevTestConsumption` too, at a fraction of the price.
`RefreshContext.rows` adds the clause so no fetcher has to remember it.

**Sort tiers by `tierMinimumUnits` and take the first that charges.** Azure
returns a tiered meter's rows in no guaranteed order, and tier 0 is often a
free allowance: Log Analytics ingestion is $0.00 up to 5 GB and $2.30 after;
Key Vault HSM keys come back as $5.00, $0.90, $2.50, $0.40 for tiers 0, 1500,
250, 4000. `unit_price()` is the correct reader.

**Match the meter name, not just the SKU.** `P80 LRS Disk` is $3,604.11 a
month, `P80 LRS Disk Mount` is $219.00 and `P80 LRS Disk Operations` is
fractions of a cent — all under `skuName` "P80 LRS". `meter_is(row, "Disk")` is
what separates them, and getting it wrong produces a plausible-looking table
that understates a large disk sixteenfold.

**A meter published without an ARM region has no per-region entry.** NAT
Gateway and Load Balancer are published against "Global"; Azure DNS against a
billing geography spelled "Zone 1", which is not an availability zone and not
an ARM region. All three go in a global section with a `scope="global"` rate
spec, not into an empty per-region one.

`main` refuses to write a table that loses or empties a section the previous
one had, because an empty section is a failed fetch rather than a price of
zero.

This file is run twice by `python -m zombiescan.pricing.refresh` — once as
`__main__` with a `FETCHERS` list of its own, once as the canonical module that
packs register into. The `__main__` block hands over to the canonical `main()`
for that reason; calling `main()` directly there rebuilds the table from core's
sections alone and writes zero prices for every pack.

### Managed disks are priced by tier, not by gigabyte

A 1 GiB Premium SSD and a 128 GiB one are both a P10 and both cost the same.
`helpers.disk_tier` maps (SKU, provisioned size) to the rung Azure bills, and
the price table is keyed by the tier *and* its redundancy — "P10 LRS", "P10
ZRS" — because zone redundancy costs about half as much again. Standard HDD has
no rung below S4, so a 4 GiB Standard disk bills as a 32 GiB one. Only
`PremiumV2_LRS` and `UltraSSD_LRS` bill per provisioned GiB, and both also bill
provisioned IOPS and throughput separately, which the finding says.

A per-GB calculation — the right answer on every other cloud — understates a
small disk by two orders of magnitude.

## The plugin and the MCP server

`plugin/` is the Claude Code plugin: two slash commands, a skill, and
`.mcp.json` pointing at the `zombiescan-mcp` console script. The server itself
is `src/zombiescan/mcp_server.py`.

**Every tool on it is read-only, and it must stay that way.** `plan_cleanup`
may call `clean.plan_for`, which only builds `Step` objects;
`clean.apply_outcome` sends them to Azure and must never be reachable from the
server. `tests/test_mcp_server.py` asserts the module names no `clean.*`
attribute beyond `plan_for` and `UNSUPPORTED`, so a tool that applies a plan
fails the suite.

Tools return computed figures — totals, counts, breakdowns, cheapest and
costliest — and echo the filter they applied. Do not add a tool that returns
rows for the caller to add up.

The protocol is JSON-RPC over stdio, standard library only. Do not add an MCP
SDK dependency for about a hundred lines of framing.

## Testing

Check logic is tested against committed JSON fixtures of real ARM responses, so
the suite runs offline with no credentials. The live smoke test hits a real
subscription and is gated behind `ZOMBIESCAN_LIVE=1`.

When adding a check, add its fixture and test in the same change —
`tests/test_packs.py` fails if a check has no test file of its own.

Fixtures are keyed the way the client returns them: `{"value": [...]}` for an
ARM list, a plain list of projected rows for a Resource Graph query. The fake
in `conftest.py` matches on **the tail of a request path**, so a sub-resource
(`/keys`, `/databases`, `/listUsages`) is keyed by its own suffix rather than
by the type it hangs off — Key Vault's vaults, keys and secrets all share a
resource type. Include a resource that is genuinely in use in every fixture;
half of what these checks do is *not* reporting things.

The live test does two things the offline suite structurally cannot: it
verifies every pinned `api-version` against ARM, and it verifies
`helpers.CONFIRMS` against the installed Azure CLI. Both are facts about the
outside world that a comment cannot keep true.

## Generated commands

Every remediation string is an `az` command built by `helpers.az`, which adds
the resource group and `--subscription` and quotes nothing you did not quote
yourself. Interpolated resource ids must be wrapped in `helpers.arg`;
`tests/test_report.py` checks both at the source.

**`--yes` is not `--quiet`.** `gcloud` takes `--quiet` on everything; `az` has
no global equivalent. `--yes` exists only on the commands that would otherwise
prompt, and passing it to one that would not is an **error**, not a no-op —
`az network nic delete --yes` fails outright. `helpers.CONFIRMS` names the
commands that take it; verify a new one with `az <command> --help` before
adding it, and the live test re-checks the whole list.

Where core `az` has no verb for a resource type — Application Insights web
tests live in an extension that is not installed by default — use
`helpers.az_resource_delete`, which drives ARM through `az resource delete
--ids`. A generated plan that needs an extension installed first is a plan that
does not run.

Validate a leaf command's syntax with `az <command> --help` before writing it;
the flags differ more than they look (`az group delete` takes `--name`, not
`--resource-group`; `az keyvault secret delete` takes `--vault-name` and no
resource group at all; `az disk delete` takes `--yes` and `az snapshot delete`
does not).
