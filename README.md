# zombiescan

[![CI](https://github.com/xbill9/zombiescan-azure/actions/workflows/ci.yml/badge.svg)](https://github.com/xbill9/zombiescan-azure/actions/workflows/ci.yml)

Find the Azure resources nobody is using, and what they cost you.

Not *"here are your 14 managed disks"* — **"9 of these are attached to nothing
and cost you $47/month, here is the plan to kill them."**

```
$ zombiescan scan --all-subscriptions

zombiescan — 18 findings across 3 subscriptions

Check                      Found  Monthly
idle-app-service-plan          2  $547.50
unattached-disk                3  $191.60
aks-idle-cluster               1   $73.00
idle-nat-gateway               1   $32.85
idle-load-balancer             1   $18.25
paused-sql-database            1   $11.50
empty-container-registry       1    $5.07
unused-public-ip               2    $3.65
orphaned-snapshot              1    $2.50
disabled-key-vault-key         1    $1.00
unused-dns-zone                1    $0.50
orphaned-nic                   1    $0.00
unused-nsg                     1    $0.00
unused-subnet                  1    $0.00

┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Location   ┃ Resource grp  ┃ Resource         ┃  Monthly ┃ Why                      ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ eastus     │ web-prod-rg   │ legacy-api-plan  │  $438.00 │ App Service plan hosts   │
│            │               │                  │          │ no apps but keeps 2 P1   │
│            │               │                  │          │ v3 Windows instance(s)   │
│            │               │                  │          │ reserved and billed      │
│ eastus     │ data-rg       │ pipeline-scratch │  $135.00 │ 1024 GiB Premium_LRS     │
│            │               │                  │          │ disk attached to no VM,  │
│            │               │                  │          │ billed as P30 LRS;       │
│            │               │                  │          │ created 412 days ago     │
│ westeurope │ platform-rg   │ staging-aks      │   $73.00 │ AKS cluster runs no      │
│            │               │                  │          │ nodes across its 2 node  │
│            │               │                  │          │ pool(s), but the         │
│            │               │                  │          │ Standard tier control    │
│            │               │                  │          │ plane is charged per     │
│            │               │                  │          │ hour regardless          │
│ eastus     │ net-rg        │ egress-gw-old    │   $32.85 │ NAT gateway is attached  │
│            │               │                  │          │ to no subnet, and Azure  │
│            │               │                  │          │ charges its hourly fee   │
│            │               │                  │          │ whether or not anything  │
│            │               │                  │          │ routes through it        │
└────────────┴───────────────┴──────────────────┴──────────┴──────────────────────────┘
showing 10 of 18, costliest first with every check represented

Estimated waste: $886.92/month ($10,643.04/year)
Estimates from list prices, not your bill. zombiescan is read-only and deleted nothing.
```

## Local-first

zombiescan runs on your machine with the Azure credentials you already have.
There is no hosted service, no account to create, no service principal to
create, no client secret to hand over, and nothing is sent anywhere. Every SaaS
tool in this space asks you to grant a role into your production subscription.
This one never asks.

**`scan` is read-only.** Read calls only. It cannot change anything, by
construction.

**`clean` deletes things**, and is a separate command for that reason — it is
not a flag you can reach by typo. It dry-runs by default, asks before each
resource, and takes a backup first wherever Azure lets it.

## Install

```
az login
uv tool install git+https://github.com/xbill9/zombiescan-azure
zombiescan scan
```

Credentials come from the Azure CLI. zombiescan asks `az` for an Azure Resource
Manager token at the start of a scan and uses it for every request, so whatever
`az login` set up — a user, a device code, a managed identity on the VM you are
on, a service principal — works, and none of it is configured here.

### Two accounts, one email

Microsoft lets one email address be **both** a work or school account and a
personal Microsoft account. They are separate directories with separate
subscriptions that happen to share a username, which is why signing in
sometimes shows you a *"Work or school account / Personal account"* picker.
`az login` takes one of them, silently.

**Sign in to each one in its own Azure CLI config directory.** The CLI looks
tokens up by username, so two identities sharing one address in the same
`~/.azure` break it for both: every `az account get-access-token` then fails
with *"Found multiple accounts with the same username"*
([azure-cli#20168](https://github.com/Azure/azure-cli/issues/20168)), and
nothing can be scanned until the cache is cleared with
`az logout --username <address>`. `AZURE_CONFIG_DIR` keeps them apart:

```
az login                                                    # pick "Work or school account"
AZURE_CONFIG_DIR=~/.azure-personal az login --tenant <id>   # pick "Personal account"
```

Pass `--tenant` for the personal account. Its subscriptions live in a directory
of its own (usually named *Default Directory*), and a plain `az login` lists
that tenant, prints a warning, and holds no token for it when the tenant
requires multi-factor authentication. The warning names the tenant id;
`--tenant` asks for MFA and signs in to it.

zombiescan runs `az` as a subprocess, so the same variable picks the account
for a scan:

```
zombiescan scan --all-subscriptions                                    # work
AZURE_CONFIG_DIR=~/.azure-personal zombiescan scan --all-subscriptions # personal
```

**`zombiescan scan --all-subscriptions` covers every tenant the CLI has signed
into** under that config directory, not just the current one. An ARM token is
issued for a single tenant, so it holds one per tenant and routes each request
to the right one — a scan that held a single token would quietly cover half of
what you can see and report less waste than there is. Identities with
different usernames share one config directory without trouble. The header
names every tenant in scope, the kind of account signed into it, and each
subscription:

```
Scanning as me@example.com
Tenant Default Directory — personal Microsoft account
  domain        meexamplecom.onmicrosoft.com
  tenant id     22222222-2222-2222-2222-222222222222
  signed in as  me@example.com
  subscription  Azure subscription 1 (default)
                33333333-3333-3333-3333-333333333333
Tenant Contoso — work or school account
  domain        contoso.onmicrosoft.com
  tenant id     11111111-1111-1111-1111-111111111111
  signed in as  me@example.com
  subscription  Engineering
                44444444-4444-4444-4444-444444444444
2 subscription(s) across 2 tenants, 22 check(s) — read-only
```

`az account list` shows both as the same user with the same email. The kind
comes from the tenant's access token: a personal Microsoft account signs in
through `live.com` and its token carries `idp: live.com`; a work or school
account in its own directory has no `idp`; a guest from another directory
names its home issuer there.

A full sweep also runs `az account list --refresh`, which costs about a second
and picks up subscriptions created since the last login.

**There is no Azure SDK in the dependency list.** `click` and `rich` are the
only two, and HTTP is `urllib` from the standard library. Every Azure call in
this tool goes to one REST surface, so a new check is an api-version entry
rather than another `azure-mgmt-*` package.

## Usage

```
zombiescan checks                         # list the checks
zombiescan providers                      # the resource providers those checks read
zombiescan scan                           # the subscription az is set to
zombiescan scan --all-subscriptions       # every enabled subscription you can see
zombiescan scan --subscription <id> --subscription <id>
zombiescan scan --location eastus         # "East US" works too
zombiescan scan --check unattached-disk --check unused-public-ip
zombiescan scan --min-cost 5              # hide findings under $5/month
zombiescan scan --limit 0                 # every finding, not just the top 25
zombiescan scan --json findings.json      # machine-readable, full detail
zombiescan scan --html report.html        # shareable report; print to PDF from a browser
zombiescan scan --script cleanup.sh       # write the plan (never runs it)
```

A full 22-check sweep of one subscription takes a few seconds.

### One pass per subscription, not per region

An AWS scanner has to fan out across every region, because almost every AWS
list call is regional. Azure does not work that way, and zombiescan does not
pretend it does:

- **An ARM list call is subscription-wide.** One request to
  `/subscriptions/<id>/providers/Microsoft.Compute/disks` returns every disk in
  every resource group in every region.
- **Azure Resource Graph answers a join in one query.** Where a check has to
  cross-reference two resource types, one KQL query does it server-side
  instead of pulling both inventories down.

So the unit of fan-out is the **subscription**. Each finding records the region
it lives in and, because no `az` command works without one, the **resource
group** — neither is derivable from the other, so both are first-class fields.

### A quiet failure Azure has and Google does not

A resource provider that has never been used on a subscription is *not
registered*, and ARM answers a list call against an unregistered provider with
**HTTP 200 and an empty page**. A scanner that simply made the call would find
nothing and report the subscription clean — a false all-clear, which is the
worst thing a tool like this can produce.

So zombiescan reads each subscription's provider registrations first, once, and
refuses to run a check whose provider is missing. Those pairs are counted under
`pairs_unavailable` and reported out loud:

```
10 of 22 subscription/check pair(s) were skipped: their resource provider is not
registered, so there is nothing of that kind here.
```

"No waste found" and "no waste found in the ten services this subscription
actually uses" are different statements. The report makes clear which one it is
making.

## The Claude Code plugin

The same engine, reachable from an agent instead of a terminal. It ships in
[`plugin/`](plugin/) and installs from this repository:

```
/plugin marketplace add xbill9/zombiescan-azure
/plugin install zombiescan@zombiescan
```

That gives you `/zombiescan` to scan, `/zombie-cleanup` to see what a cleanup
would do, and a skill that picks itself up whenever the conversation turns to
Azure spend. Behind them is an MCP server with five tools:

| Tool | What it does |
| --- | --- |
| `list_checks` | What the scanner looks for. No credentials needed. |
| `scan_subscription` | Scans, prices, writes the JSON report, returns the totals |
| `estimate_savings` | Totals and breakdowns over a report, with a filter |
| `explain_finding` | Why a resource counts as waste and what keeping it costs |
| `plan_cleanup` | The requests a cleanup would send, and which have no undo |

**Every tool is read-only.** `plan_cleanup` builds the same plan `clean` shows
on a dry run and stops there; `clean.apply_outcome`, the one function that
sends a step to Azure, is not reachable from the server, and the suite asserts
that the module does not name it. Applying a plan stays `zombiescan clean
--apply` at your own terminal, where the per-resource prompt and the
irreversible-step warning are.

The tools return figures already computed — totals, counts, per-check,
per-location, per-subscription and per-resource-group breakdowns, the cheapest
and costliest row — and echo the filter they applied under `filter_applied`. A
filter naming a check that is not in the report returns an exact, sourced zero,
which reads like good news; the echo carries `no_such_checks_in_report` so the
mistake is visible in the answer rather than in next month's bill.

The server speaks JSON-RPC over stdio using the standard library alone, so
installing zombiescan does not pull an MCP SDK in behind it. It can also be run
directly:

```
zombiescan-mcp     # or: uv run zombiescan-mcp
```

## Checks

Twenty-two checks in two packs. `core` covers the services almost every
subscription uses; `aks` is a separate pack because Kubernetes Service is a
whole service with its own provider and its own pricing.

| Check | Finds | Costs money |
| --- | --- | --- |
| `unattached-disk` | Managed disks in `Unattached` state | yes, at the disk's **tier** rate |
| `deallocated-vm` | VMs stopped or deallocated | the disks and public IPs they keep |
| `orphaned-snapshot` | Snapshots whose source disk is gone | per stored GB-month, as a ceiling |
| `unused-image` | Managed images nothing boots from | per stored GB-month |
| `unused-public-ip` | Static public IPs attached to nothing | yes, at the ordinary rate; $0 when carved from a prefix, which bills per address |
| `idle-nat-gateway` | NAT gateways with no subnet | **yes, ~$32.85/month flat** |
| `idle-load-balancer` | Standard load balancers with empty backend pools | the included-rules charge plus frontend public IPs |
| `orphaned-nic` | Network interfaces belonging to no VM | the public IPs it holds; it also blocks other deletions |
| `unused-nsg` | Security groups on no subnet and no NIC | no |
| `unused-subnet` | Subnets with nothing in them | no — the IP range is the cost |
| `empty-vnet` | Virtual networks with no NIC in any subnet | whatever priced waste is inside |
| `idle-app-service-plan` | Plans hosting no apps | **yes, the full reserved instances** |
| `paused-sql-database` | Serverless databases paused but still stored | provisioned storage |
| `unused-dns-zone` | Public zones holding only SOA and NS | yes, at the marginal tier |
| `stale-key-vault-secret` | Secrets with no new version in 90 days | no — Key Vault bills per operation |
| `disabled-key-vault-key` | Keys disabled but not deleted | yes for HSM keys, free for software |
| `empty-container-registry` | Registries holding no images | yes — the tier fee is flat |
| `unbounded-log-workspace` | Workspaces with no daily cap and long retention | unpriced; reported as growth |
| `unmanaged-storage-account` | Versioned accounts with no lifecycle policy | unpriced; reported as growth |
| `unused-availability-test` | Web tests watching a deleted component | no — reported for the alerts |
| `empty-resource-group` | Resource groups containing nothing | no |
| `aks-idle-cluster` | AKS clusters running no nodes | **$73/month on Standard, $0 on Free** |

### Four that behave differently from what people expect

- **An idle NAT gateway is expensive.** Azure bills it a flat ~$0.045/hour —
  about $32.85 a month — from creation to deletion, whether or not a subnet is
  attached. That is the AWS shape, and the *reverse* of Google's Cloud NAT,
  which bills gateway uptime per VM using it and so costs almost nothing when
  idle.
- **An idle AKS cluster may be free.** Only the Standard and Premium tiers pay
  for a control plane; a **Free**-tier cluster scaled to zero costs nothing at
  all. That is the reverse of GKE, where the management fee is charged whatever
  the cluster is doing. The check prices each cluster at its own tier and says
  which it found.
- **A managed disk is billed by tier, not by gigabyte.** A 1 GiB Premium SSD
  and a 128 GiB one are both a P10 and both cost $19.71 a month in `eastus`.
  Shrinking a disk saves nothing until it crosses a rung; deleting it saves
  everything. Only Premium SSD v2 and Ultra bill per provisioned GiB.
- **An unattached public IP costs the same as an attached one.** Azure charges
  one hourly rate either way, so nothing about the price signals that an
  address is idle — unlike Google, which charges *more* for a reserved address
  attached to nothing. What makes an Azure address waste is simply that it is
  still reserved.

## About the numbers

Costs are estimates from pay-as-you-go **list prices**, not from your bill.
They ignore reservations, savings plans, Azure Hybrid Benefit, dev/test rates,
enterprise agreement pricing and credits.

Prices come from the [Azure Retail Prices
API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices),
not from anyone's memory, and are regenerated with:

```
uv run python -m zombiescan.pricing.refresh
```

That API is public: no credentials, no subscription, no `az login`. The table
is per-region where Azure prices per region, because region moves the number.

**Reading the catalog correctly is the subtle part**, and it fails silently
four different ways. Every one of these was measured against the live API
rather than recalled:

- **`priceType` must be `Consumption`.** The same meter is published as
  `Reservation` and `DevTestConsumption` too, at a fraction of the price.
- **A tiered meter's rows come back in no particular order, and the first
  tier is often free.** Log Analytics ingestion is published as $0.00 up to 5
  GB and $2.30 after it. Key Vault HSM keys come back as $5.00, $0.90, $2.50
  and $0.40, tiered at 0, 1500, 250 and 4000. The refresher sorts by
  `tierMinimumUnits` and takes the first tier that actually charges.
- **The meter name distinguishes the capacity charge from its neighbours.**
  `P80 LRS Disk` is $3,604.11 a month; `P80 LRS Disk Mount` is $219.00 and
  `P80 LRS Disk Operations` is fractions of a cent. All three carry the same
  SKU name, so matching on the SKU alone understates a large disk sixteenfold
  and still looks like a working price table.
- **Some meters have no ARM region at all.** NAT Gateway and Load Balancer are
  published against `armRegionName` "Global"; Azure DNS against a billing
  geography spelled "Zone 1", which shares a word with availability zones and
  means something unrelated. A per-region fetcher returns an empty section for
  all three.

The refresher refuses to write a table that empties or drops a section the
previous one had, because an empty section is a failed fetch rather than a
price of zero.

A `~` next to a cost means it is an estimate or an upper bound — the region had
no price entry, the SKU was unrecognised, or the resource bills on what it uses
rather than on what it provisioned. Every finding carries a `note` in `--json`
output saying which.

## What it deliberately does not flag

Checks would rather miss waste than invent it. A false positive here costs
someone an outage.

- A disk reporting `Unattached` while `managedBy` still names an owner belongs
  to something that is not a VM — a disk pool, a restore in flight.
- A NIC with no `virtualMachine` may back a private endpoint or a private link
  service, both of which are emphatically in use.
- A subnet with a **delegation** hands address management to a service that
  then uses it, and reports no IP configurations while doing so.
- `GatewaySubnet`, `AzureFirewallSubnet` and `AzureBastionSubnet` are named by
  Azure and exist to be empty until the gateway lands in them.
- A resource group Azure manages on another resource's behalf — an AKS node
  group scaled to zero is the common one — is empty because its cluster is
  idle, not because it is abandoned.
- A container registry that refuses the usage call is behind a firewall, not
  empty; a failed read is not evidence.
- An image can be referenced by a scale set, a gallery version, a deployment
  template or a VM in another subscription, none of which this scan can see.
- A Log Analytics workspace with a daily cap, or with short retention, has had
  a decision made about it.

Two checks report **staleness**, which is a prompt to look rather than a
verdict. `stale-key-vault-secret` is judged on the age of the newest version,
because the management plane does not report access times; the finding says so
rather than claiming the secret is unread. It is also explicitly priced at
zero — Key Vault bills per operation, not per stored secret — so the reason to
act on it is that an unrotated credential is a credential, not that it is
expensive.

## Cleaning up

`scan` tells you what to delete. `clean` does it.

```
zombiescan clean                          # dry run: prints the exact requests, changes nothing
zombiescan clean --apply                  # asks before each resource
zombiescan clean --apply --yes            # no prompts
zombiescan clean --from findings.json     # act on a report you have already read
zombiescan clean --check unused-public-ip --apply
zombiescan clean --apply --audit audit.json
```

**Dry run is the default and it is exact.** `--apply` changes one thing:
whether a planned request is sent. It does not change which requests get
planned, so what the dry run shows is what the real run does.

**Backups come first where Azure allows one.** A disk is snapshotted before
deletion — incrementally, so it costs very little — and if that step fails, the
delete that assumed it does not run.

**Irreversible steps are labelled, and Azure has fewer of them than most
clouds.** The flag marks the cases with no recovery window at all:

- Releasing a **public IP address** is final. Azure returns it to the regional
  pool and will not hand the same one back, so anything with it in a DNS
  record or a partner's allow-list breaks.
- Deleting an **orphaned snapshot** is final: the snapshot *is* the backup, and
  its source disk is already gone.
- Deleting an **AKS cluster** destroys the control plane and every object in
  it.

Deleting a **Key Vault key** is *not* marked: soft-delete is mandatory and
keeps it recoverable for the vault's retention period — which also means the
key keeps being billed until that window closes, so the saving starts later
than the command returns. Deleting a **SQL database** is not marked either: it
restores from point-in-time backups for the server's retention period.

**Deleting a VM keeps its disks.** ARM detaches rather than deletes a managed
disk by default, so the OS and data disks survive and show up as
`unattached-disk` on the next scan, with a snapshot-first plan of their own.

**An empty resource group is re-checked before it is planned.** `az group
delete` removes everything inside without listing what that was, and a scan is
a snapshot — something can be deployed into the group between the scan and the
apply. The cleaner lists the group again during planning and refuses if
anything has appeared.

**`--audit` writes a record** of every request attempted, its body, its result
and what it saved — the file you will want when someone asks what happened.

Five findings have no cleaner, and say why instead of guessing: `empty-vnet`
(the deletion order depends on what else references what), `unused-image` (a
reference this scan cannot see would break the next scale-out),
`stale-key-vault-secret` (deleting a live credential takes an application
down), `unbounded-log-workspace` and `unmanaged-storage-account` (both are
judgements about what must be kept, and a cap set too low silently drops the
logs an incident would be investigated from).

### Permissions for cleaning

`clean --apply` needs write access, which is a different posture from scanning.
The read-only role in [`policy/`](policy/) deliberately cannot delete anything.
Grant the delete permissions to a separate principal, scoped to what you intend
to remove.

## The generated script

`--script` writes a plan. zombiescan never runs it, and has no flag that will.

Every value interpolated into a command is shell-quoted, so a resource name
cannot become a command in the file you are about to execute. Azure's own
naming rules make that unreachable today, but a tool whose whole proposition is
handing you a script to run should not depend on a remote service's input
validation for local shell safety. Resource *group* names are the ones to watch:
Azure allows parentheses and periods in them.

Every generated command carries `--subscription`, so pasting one into a shell
pointed at a different subscription deletes nothing by surprise.

**`--yes` is added only where the command takes it.** `az` has no global
equivalent of `gcloud --quiet`: `--yes` exists on the commands that would
otherwise prompt and nowhere else, and passing it to one that would not is an
error rather than a no-op — `az network nic delete --yes` fails outright. The
list of commands that accept it lives in `helpers.CONFIRMS` and the live test
re-checks it against the Azure CLI you actually have installed.

## Output formats

**`--json`** is a versioned contract, not a dump. The schema lives at
[`docs/findings.schema.json`](docs/findings.schema.json) and the suite
validates against it. Check `schema_version` before parsing: it is `4`, and
findings carry `subscription`, `resource_group`, `location` and the full
`arm_id`.

**`--html`** is a single self-contained file — no stylesheet, no font, no
script, no network request — with a print stylesheet, because printing to PDF
from a browser is how most people will produce a PDF.

**`--script`** is the cleanup plan as bash, commented with what each command
saves and why the resource was flagged.

## Exit codes

`0` on success, including when a scan finds nothing. `1` when every
subscription/check pair failed — a scan that reached nothing found nothing, and
exiting `0` would tell a CI job the subscription was clean. `2` for a bad
invocation or missing credentials.

A check whose resource provider is not registered on a subscription is **not** a
failure. It is counted under `pairs_unavailable` and skipped, because a
subscription that has never used App Service has no App Service waste.

## Permissions

[`policy/zombiescan-scanner-role.json`](policy/zombiescan-scanner-role.json) is
an Azure custom role holding exactly the read actions the checks use.
`zombiescan providers` prints the providers behind them, and the suite fails if
a check declares a provider the role does not cover — or if the role grants any
action that is not a read.

It has not been verified under a principal actually constrained by it —
credentials with Owner or Contributor are not limited the way the role
describes. Assign it to a dedicated service principal and scan with that before
relying on the claim.

## Development

```
uv sync
uv run pytest                               # offline, fixtures, no credentials
ZOMBIESCAN_LIVE=1 uv run pytest -m live     # one real scan, opt-in
uv run ruff format . && uv run ruff check --fix .
make help                                   # the rest
```

Checks are tested against committed JSON fixtures of real ARM responses, so the
suite runs offline with no credentials. Every check has a fixture and a test
file of its own, and `tests/test_packs.py` fails if one does not.

The live test does two things the offline suite structurally cannot: it
verifies every pinned `api-version` against what ARM currently accepts, and it
verifies the `--yes` list against the installed Azure CLI. Both are facts about
the outside world that a comment cannot keep true.

### Packs

Checks live in packs under `src/zombiescan/packs/<pack>/`, discovered by
existing rather than listed in an import block. A pack owns its checks, its
cleaners, its price rates and the fetchers that refresh them. `core` and `aks`
are built in and load through the same path as a pack installed from PyPI, so
the seam is exercised on every run rather than only by third parties.

[`docs/PACKS.md`](docs/PACKS.md) is the pack-author contract.

**A pack is code that runs with your Azure credentials**, and nothing here
verifies that its checks only read. The read-only guarantee holds for the packs
in this repository because they are reviewed. Install third-party packs on the
same judgement you would apply to any other dependency.

## License

MIT.
