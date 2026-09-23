# zombiescan — build plan

**Scope decision:** local only. No hosted site, no publish endpoint. The scan
runs on the local machine against the local Azure CLI credentials.

## What it is

An open-source, local-first Azure waste scanner. It runs on your machine with
your existing `az login`, finds resources you are paying for that nothing is
using, prices them, and prints a report with a dollar total.

Positioning: not "here are your 14 managed disks" but "9 of these are attached
to nothing and cost you $47/month; here is the plan to kill them."

Two front doors, one engine:

1. **CLI** — `zombiescan scan --all-subscriptions`
2. **Claude Code plugin** — a skill plus an MCP server, so the agent can run a
   scan, explain a finding, and draft the cleanup itself

Nothing leaves the machine. No service principal handover, no findings uploaded
anywhere. That is the whole privacy story and it is a real advantage over every
SaaS tool in this space.

## The transport, and why there is no SDK

Credentials come from the `az` CLI; everything after that is Azure Resource
Manager over HTTPS with `urllib`. The dependency list is `click` and `rich`.

That is a deliberate choice rather than a shortcut. The architecture above the
client layer — `building.simple_check`, the `Step` runner in `clean.py` — works
because every service is reached the same way: a path, a pinned `api-version`,
and a `value`/`nextLink` page. The `azure-mgmt-*` libraries would each bring
their own client shape and their own pagination idiom, and the generic runner
could not drive them.

One `az account get-access-token` **per tenant**, every one of them fetched
single-threaded before any worker exists and refreshed behind a lock, is the
whole credential story.

Per tenant rather than per scan because an ARM token is issued for exactly one
directory. Microsoft lets a single email address be both a work or school
account and a personal Microsoft account, so one person having two tenants is
ordinary rather than an enterprise edge case — and a scanner holding one token
would sweep whichever tenant happened to be current and report the other's
waste as absent. The subscription list therefore comes from `az account list
--all`, which spans every identity the CLI has signed into, rather than from
ARM's own `/subscriptions`, which cannot see past its token's tenant.

Each request picks its token by reading the subscription out of its own ARM
path, so nothing above the client layer has to thread a tenant through.
Resource Graph is the one exception — its path names no subscription, because
they go in the body — and it names its tenant explicitly.

## The unit of fan-out is the subscription

This is the one structural difference from an AWS scanner, and it shapes
everything above the client layer.

Almost every AWS list call is regional, so an AWS scanner must fan out across
seventeen regions and multiply every check by that. Azure publishes two things
that remove the need:

- **An ARM list call is subscription-wide.**
  `/subscriptions/<id>/providers/Microsoft.Compute/disks` returns every disk in
  every resource group in every region, in one call.
- **Azure Resource Graph** answers a cross-resource-type join in a single KQL
  query, so a check that needs to know "which NICs have a VM" does not pull two
  full inventories down to find out.

So a check runs once per subscription and reads each finding's location off the
resource. `Finding.location` holds a region or `global`. `Finding.subscription`
is the account-equivalent. `Finding.resource_group` is Azure's own doing: no
`az` command works without it, so a finding that did not carry one would
produce a remediation nobody could run.

## The failure mode the design is built around

**A resource provider that is not registered on a subscription returns an empty
page with HTTP 200, not an error.**

This is the single most important fact about scanning Azure, because it turns
"this subscription has never used App Service" into "this subscription has no
App Service waste" with no signal in between. A scanner that simply made the
call would report a clean subscription, which is the worst output a tool like
this can produce.

So:

- Every check declares the resource provider namespaces it reads.
- `Arm.registered_providers` reads each subscription's registrations once.
- The engine refuses to run a check whose provider is missing, counts the pair
  under `pairs_unavailable`, and the report says how many were skipped.

The same declaration generates `zombiescan providers` and the read-only role in
`policy/`, and the suite fails if any of the three drift apart.

## The zombie catalog

Each check returns: resource id, subscription, resource group, location, why it
is considered waste, estimated monthly cost, the full ARM id, and a suggested
`az` command. The commands are printed; `clean` is a separate command that runs
them.

### core pack

| Check | Why it's waste | Rough monthly cost |
| --- | --- | --- |
| ✅ Unattached managed disks | Billed in full while attached to nothing | by tier: $0.60 (P1) to $3,604 (P80) |
| ✅ Stopped and deallocated VMs | The compute is free, the disks and IPs are not | disk and public IP cost |
| ✅ Idle dedicated hosts | Billed per host, VMs or no VMs | **$600–$40,000/month** by host SKU |
| ✅ Unused capacity reservations | Every reserved slot bills at the VM rate, used or not | the VM size's rate per unused slot |
| ✅ Orphaned snapshots | Source disk gone | ~$0.05/GB, as a ceiling |
| ✅ Unused managed images | Nothing boots from them | ~$0.05/GB |
| ✅ Unused public IPs | Billed the same idle as in use | ~$3.65 each |
| ✅ Idle NAT gateways | Flat hourly fee, subnet or no subnet | **$32.85/month** |
| ✅ Idle load balancers | Standard SKU pays for its rules regardless | ~$18.25/month plus frontend IPs |
| ✅ Orphaned NICs | Holds billed IPs; blocks deleting the IP, subnet and VNet | the IPs it holds, ~$3.65 each |
| ✅ Unused NSGs | Rules attached to nothing, read as protection | $0 (hygiene) |
| ✅ Unused subnets | IP range reserved against nothing | $0 (blocks reuse) |
| ✅ Empty VNets | Nothing running inside | the priced waste within |
| ✅ Idle App Service plans | Instances reserved with no app on them | **$110–$440/month** |
| ✅ Paused SQL databases | Paused, but storage still bills | ~$0.115/GB |
| ✅ Unused DNS zones | Only the SOA and NS records | $0.50 at the first tier |
| ✅ Stale Key Vault secrets | No new version in 90 days | $0 — Key Vault bills per operation |
| ✅ Disabled Key Vault keys | Disabling does not stop the charge | $1.00 HSM, $0 software |
| ✅ Empty container registries | Tier fee is flat, contents irrelevant | $5 / $20 / $50 by tier |
| ✅ Unbounded log workspaces | No daily cap, long retention | unpriced; reported as growth |
| ✅ Unmanaged storage accounts | Versioning on, no lifecycle policy | unpriced; reported as growth |
| ✅ Unused availability tests | Watching a deleted component | $0 (the alerts are the cost) |
| ✅ Empty resource groups | Nothing inside | $0 (hygiene) |
| ✅ Idle provisioned model deployments | Every PTU bills hourly; no requests in 7 days | **$1–$2.72 per PTU-hour** |
| ✅ Empty AI Services accounts | No deployment and no project | $0 (hygiene) |
| ✅ Idle container apps | `minReplicas` ≥ 1, no requests in 7 days | idle vCPU and memory rate per replica |
| ✅ Empty Container Apps environments | No app inside | $0 Consumption; Dedicated instances + $73 fee |
| ✅ Idle workload profiles | Dedicated instances with no app on them | ~$225/month per D4 instance |
| ✅ Idle ML compute | Instance with no idle shutdown; cluster minimum above zero | the VM rate per node |

### aks pack

| Check | Why it's waste | Rough monthly cost |
| --- | --- | --- |
| ✅ Idle AKS clusters | Control plane charged by SKU tier | **$73/month Standard, $0 Free** |

AKS is a separate pack as the proof the seam carries a whole service: its own
resource provider (`Microsoft.ContainerService`), its own rate section, and its
own fetcher against a meter core never looks at.

**Four findings behave differently from the instinct people bring**, and the
checks say so rather than leaving the reader to assume:

- **An idle NAT gateway is expensive.** Azure bills it a flat hourly fee the
  way AWS does — the reverse of Google's Cloud NAT, which bills per VM behind
  it and so costs almost nothing idle.
- **An idle AKS cluster may be free.** Only Standard and Premium pay for a
  control plane. A Free-tier cluster scaled to zero costs nothing, which is the
  reverse of GKE.
- **A managed disk is billed by tier, not by gigabyte.** A 1 GiB Premium SSD
  and a 128 GiB one are both a P10 at the same price. Only Premium SSD v2 and
  Ultra bill per provisioned GiB.
- **An unattached public IP costs the same as an attached one**, so nothing in
  the price signals that it is idle — unlike Google, where a reserved address
  costs *more* than one in use.

Two checks have no Google Cloud equivalent at all, and both are Azure-shaped:
**orphaned NICs**, because a Compute Engine network interface is a property of
its instance and cannot outlive one, and **empty resource groups**, because a
project is the thing you delete rather than a container inside one.

## Pricing

Prices come from the **Azure Retail Prices API** (`prices.azure.com`), which is
public — no credentials, no subscription — and are bundled as `table.json` so a
scan works offline and adds no latency.

Each fetcher states the exact service, product and meter it matches, and a
matcher that stops matching yields an empty section — which `main` refuses to
write over a populated one.

Four traps, all silent, all measured against the live API:

- **`priceType` must be `Consumption`.** The same meter is also published as
  `Reservation` and `DevTestConsumption`, at a fraction of the price.
- **Tier rows come back unordered, and tier 0 is often free.** Log Analytics
  ingestion is $0.00 up to 5 GB and $2.30 after. Key Vault HSM keys come back
  as $5.00, $0.90, $2.50, $0.40 for tiers 0, 1500, 250, 4000. `unit_price`
  sorts by `tierMinimumUnits` and takes the first tier that charges.
- **The meter name separates capacity from its neighbours.** `P80 LRS Disk` is
  $3,604.11; `P80 LRS Disk Mount` is $219.00 and `P80 LRS Disk Operations` is
  fractions of a cent, all under the same SKU name.
- **Some meters have no ARM region.** NAT Gateway and Load Balancer are
  published against "Global"; Azure DNS against a billing geography spelled
  "Zone 1", which is not an availability zone and not an ARM region.

Verified rates in `eastus` at the time of writing: P10 disk $19.71/month, P30
$135.17, P80 $3,604.11, S4 $1.536, E10 $9.60; Premium SSD v2 $0.081/GiB-month;
snapshots $0.05/GB; hot LRS blob $0.0208/GB; static public IP $0.005/hour; NAT
Gateway $0.045/hour; Standard load balancer rules $0.025/hour; App Service P1
v3 $0.315/hour Windows and $0.155 Linux; SQL General Purpose storage
$0.115/GB; public DNS zone $0.50 for the first 25; Key Vault HSM key $1.00;
Container Registry $0.1666/$0.6666/$1.6666 per day; Log Analytics ingestion
$2.30/GB and retention $0.10/GB-month; AKS Standard $0.10/hour; dedicated host DSv3-Type3 $4.225/hour; D2s_v3
Linux compute $0.096/hour.

**RBAC posture:** read-only. `policy/zombiescan-scanner-role.json` is a custom
role holding exactly the read actions the checks use, generated from the
`providers=` each check declares and kept in step by the suite.

## Architecture

```
your laptop
-----------
az login
        |  az account get-access-token, fetched once up front and locked thereafter
        v
zombiescan engine (python, stdlib http)
  multi-subscription, parallel, read-only
  azure.py      ARM REST, Resource Graph, provider registration, ARM id parsing
  packs/        one module per zombie check, + cleaners.py
  pricing/      bundled price table, rate registry, refresh script
  engine.py     subscription fan-out, the provider pre-check, credentials
  report.py     terminal table, JSON, remediation script
  cli.py        click entry point
        |
        +--> terminal report (rich table, dollar total)
        +--> findings.json  (schema_version 4)
        +--> report.html    (self-contained, print-to-PDF)
        +--> cleanup.sh     (printed, never executed)
        |
        v
Claude Code plugin
  skill + slash commands + MCP server
  tools: list_checks, scan_subscription, estimate_savings, explain_finding, plan_cleanup
```

**Repo layout**

```
zombiescan-azure/
  src/zombiescan/
    azure.py      ARM client, credentials, Resource Graph, id parsing
    packs/        pack manifest, discovery, API version
      core/         one module per zombie check, + cleaners.py
      aks/          checks, cleaners, rates.py, refresh.py
    pricing/      bundled price table, rate registry, refresh script
    building.py   simple_check, for checks that are one call and one filter
    helpers.py    shared helpers, public to packs
    engine.py     subscription fan-out, provider registration, credentials
    report.py     terminal table, JSON, remediation script
    html.py       self-contained HTML report
    clean.py      the plan runner
    cli.py        click entry point
  docs/PACKS.md   the pack-author contract
  plugin/         Claude Code plugin: skill, slash commands, MCP server
  policy/         minimal read-only custom role
  tests/          check logic against recorded fixtures
```

### Two things that must not regress

1. **The provider registration pre-check.** Without it, a check against an
   unregistered provider returns an empty page and the subscription reads as
   clean. It is not an optimisation and removing it on the grounds that "the
   call works" is the single worst change that could be made here.

2. **The api-version pins.** Azure has no "latest": the version is a required
   query parameter, and one that has been retired produces
   `InvalidResourceType`, which is 404-shaped and therefore counted as
   *unavailable* rather than raised. The check silently stops running. The live
   test verifies every pin against what ARM currently accepts, because nothing
   offline can.

Neither is covered by the offline suite, because neither reproduces without a
real subscription.

## Distribution

MIT on GitHub, installable with `uv tool install git+...` or `pipx`, plus the
Claude Code plugin in the same repo. `.claude-plugin/marketplace.json` at the
repo root makes it installable with
`/plugin marketplace add xbill9/zombiescan-azure`.

## Wanted but not built

- **More metric-driven checks** — idle SQL by connection count,
  over-provisioned Cosmos DB, VMs at 2% CPU, Key Vault keys unused per
  diagnostic log. `helpers.metric_totals` reads an Azure Monitor platform
  metric over a 7-day window; `idle-provisioned-deployment` and
  `idle-container-app` use it, and each of these is one more caller.
- **Management-group scanning.** `--all-subscriptions` uses ARM's subscription
  list, which covers what the caller can see. Walking a management-group
  hierarchy deliberately, with per-group totals, is a different shape.
- **Reservation and savings-plan awareness.** Every figure here is list price.
  A subscription with a reservation is overcharged by this report, and saying
  by how much needs Cost Management exports rather than the retail catalog.
- **One Resource Graph query across every subscription at once.** Graph can do
  it, and it would collapse a fifty-subscription scan into one call per check.
  The per-subscription fan-out is kept because it isolates a permission failure
  to the subscription that caused it.

## Open questions

- Whether `--all-subscriptions` should default on. Off is faster and safer; on
  is what people actually want, because the forgotten resources are always in
  the subscription nobody opens.
- Whether `unused-image` should read Azure Compute Gallery versions as well as
  managed images. It would widen the check to the place images actually live
  now, at the cost of a second list call and a more complicated reference test.
