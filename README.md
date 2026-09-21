# zombiescan

[![CI](https://github.com/xbill9/zombiescan-gcp/actions/workflows/ci.yml/badge.svg)](https://github.com/xbill9/zombiescan-gcp/actions/workflows/ci.yml)

Find the Google Cloud resources nobody is using, and what they cost you.

Not *"here are your 14 Persistent Disks"* — **"9 of these are attached to
nothing and cost you $47/month, here is the plan to kill them."**

```
$ zombiescan scan --all-projects

zombiescan — 18 findings across 3 projects

Check                      Found  Monthly
idle-filestore                 1  $256.00
unattached-disk                3  $155.00
gke-idle-cluster               1   $73.00
stopped-sql-instance           1   $85.00
stopped-instance               1   $60.00
idle-forwarding-rule           1   $18.25
unused-static-ip               2   $14.60
stale-artifact-repository      1    $6.40
orphaned-snapshot              1    $5.00
unused-dns-zone                1    $0.20
stale-secret                   1    $0.06
disabled-kms-key               1    $0.06
empty-vpc-network              1    $0.00
unused-subnet                  1    $0.00
unused-firewall-rule           1    $0.00
unbounded-log-bucket           1    $0.00

┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Project     ┃ Location      ┃ Resource             ┃  Monthly ┃ Why                      ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ data-eng    │ us-central1-a │ ml-scratch           │  $256.00 │ 1024 GB ZONAL Filestore  │
│             │               │                      │          │ instance on network      │
│             │               │                      │          │ 'research', which runs   │
│             │               │                      │          │ no instances that could  │
│             │               │                      │          │ mount it                 │
│ analytics   │ us-central1   │ old-reporting-db     │   $85.00 │ Cloud SQL instance is    │
│             │               │                      │          │ stopped, but its 500 GB  │
│             │               │                      │          │ of SSD storage still     │
│             │               │                      │          │ bills                    │
│ data-eng    │ us-central1-a │ pipeline-scratch     │   $85.00 │ 500 GB pd-ssd disk       │
│             │               │                      │          │ attached to no instance; │
│             │               │                      │          │ created 412 days ago     │
│ platform    │ us-central1-a │ staging-cluster      │   $73.00 │ GKE cluster runs no      │
│             │               │                      │          │ nodes across its 2 node  │
│             │               │                      │          │ pool(s), but the cluster │
│             │               │                      │          │ management fee is        │
│             │               │                      │          │ charged per hour         │
│             │               │                      │          │ regardless               │
└─────────────┴───────────────┴──────────────────────┴──────────┴──────────────────────────┘
showing 10 of 18, costliest first with every check represented

Estimated waste: $673.57/month ($8,082.84/year)
~ marks an estimate or upper bound; the per-finding 'note' in --json output says why.
Estimates from list prices, not your bill. zombiescan is read-only and deleted nothing.
```

## Local-first

zombiescan runs on your machine with the Google Cloud credentials you already
have. There is no hosted service, no account to create, no service account key
to hand over, and nothing is sent anywhere. Every SaaS tool in this space asks
you to grant a role into your production project. This one never asks.

**`scan` is read-only.** List and get calls only. It cannot change anything, by
construction.

**`clean` deletes things**, and is a separate command for that reason — it is
not a flag you can reach by typo. It dry-runs by default, asks before each
resource, and takes a backup first wherever Google lets it.

## Install

```
gcloud auth application-default login
uv tool install git+https://github.com/xbill9/zombiescan-gcp
zombiescan scan
```

Credentials are Application Default Credentials: whatever
`google.auth.default()` finds. A user login, a service account attached to the
VM you are on, a key file named by `GOOGLE_APPLICATION_CREDENTIALS` — all work,
and none of them is configured here.

## Usage

```
zombiescan checks                     # list the checks
zombiescan apis                       # the APIs those checks need enabled
zombiescan scan                       # the project gcloud is configured for
zombiescan scan --all-projects        # every active project you can see
zombiescan scan --project prod-1 --project prod-2
zombiescan scan --location us-central1    # keeps us-central1-a, -b, -c too
zombiescan scan --check unattached-disk --check unused-static-ip
zombiescan scan --min-cost 5          # hide findings under $5/month
zombiescan scan --limit 0             # every finding, not just the top 25
zombiescan scan --json findings.json  # machine-readable, full detail
zombiescan scan --html report.html    # shareable report; print to PDF from a browser
zombiescan scan --script cleanup.sh   # write the plan (never runs it)
```

A full 20-check sweep of one project takes a few seconds.

### One pass per project, not per region

An AWS scanner has to fan out across every region, because almost every AWS
list call is regional. Google Cloud does not work that way, and zombiescan
does not pretend it does:

- **Compute Engine has `aggregatedList`.** One call returns disks, addresses,
  instances or routers across every zone and region at once.
- **Most other APIs accept `locations/-`**, a wildcard meaning every location.

So the unit of fan-out is the **project**. Each finding still records the exact
`location` it lives in — a zone, a region, or `global` — because that is what
you need to delete it, and `--location` filters on that.

Two APIs reject the wildcard and are walked location by location instead: Cloud
KMS and Artifact Registry. Those checks enumerate locations themselves and
fetch them in parallel.

## The Claude Code plugin

The same engine, reachable from an agent instead of a terminal. It ships in
[`plugin/`](plugin/) and installs from this repository:

```
/plugin marketplace add xbill9/zombiescan-gcp
/plugin install zombiescan@zombiescan
```

That gives you `/zombiescan` to scan, `/zombie-cleanup` to see what a cleanup
would do, and a skill that picks itself up whenever the conversation turns to
Google Cloud spend. Behind them is an MCP server with five tools:

| Tool | What it does |
| --- | --- |
| `list_checks` | What the scanner looks for. No credentials needed. |
| `scan_project` | Scans, prices, writes the JSON report, returns the totals |
| `estimate_savings` | Totals and breakdowns over a report, with a filter |
| `explain_finding` | Why a resource counts as waste and what keeping it costs |
| `plan_cleanup` | The calls a cleanup would make, and which have no undo |

**Every tool is read-only.** `plan_cleanup` builds the same plan `clean` shows
on a dry run and stops there; `clean.apply_outcome`, the one function that
sends a step to Google Cloud, is not reachable from the server, and the suite
asserts that the module does not name it. Applying a plan stays `zombiescan
clean --apply` at your own terminal, where the per-resource prompt and the
irreversible-step warning are.

The tools return figures already computed — totals, counts, per-check,
per-location and per-project breakdowns, the cheapest and costliest row — and
echo the filter they applied under `filter_applied`. A filter naming a check
that is not in the report returns an exact, sourced zero, which reads like good
news; the echo carries `no_such_checks_in_report` so the mistake is visible in
the answer rather than in next month's bill.

The server speaks JSON-RPC over stdio using the standard library alone, so
installing zombiescan does not pull an MCP SDK in behind it. It can also be run
directly:

```
zombiescan-mcp     # or: uv run zombiescan-mcp
```

## Checks

Twenty checks in two packs. `core` covers the services almost every project
uses; `gke` is a separate pack because Kubernetes Engine is a whole service
with its own API and its own pricing.

| Check | Finds | Costs money |
| --- | --- | --- |
| `unattached-disk` | Persistent Disks with no `users` | per GB-month, by disk type |
| `unused-static-ip` | Reserved external IPs attached to nothing | yes, at the *idle* rate |
| `stopped-instance` | TERMINATED or SUSPENDED VMs | the disks they keep |
| `orphaned-snapshot` | Snapshots whose source disk is gone | per stored GB-month |
| `unused-image` | Custom images nothing boots from | per stored GB-month |
| `idle-cloud-nat` | Cloud NAT in a network with no VMs | the addresses it reserves |
| `idle-forwarding-rule` | Load balancers with no backends | the forwarding rule minimum |
| `unused-subnet` | Subnets in networks running nothing | no — the IP range is the cost |
| `unused-firewall-rule` | Rules disabled, or targeting a tag nothing carries | no |
| `empty-vpc-network` | Networks with nothing running in them | whatever priced waste is inside |
| `stopped-sql-instance` | Cloud SQL stopped but still storing | storage, doubled if regional |
| `unused-dns-zone` | Managed zones holding only SOA and NS | yes, at the marginal tier |
| `stale-secret` | Secrets with no new version in 90 days | per enabled version, per replica |
| `disabled-kms-key` | Key versions disabled but not destroyed | yes — disabling does not stop it |
| `idle-filestore` | Filestore in a network with no compute | per provisioned GB-month |
| `stale-artifact-repository` | Repositories with no push in 90 days | per GB-month, as an upper bound |
| `unbounded-log-bucket` | Log buckets that never expire their contents | unpriced; reported as growth |
| `unmanaged-gcs-bucket` | Versioned buckets with no lifecycle rule | unpriced; reported as growth |
| `unused-uptime-check` | Uptime checks watching deleted VMs | no — reported for the alerts |
| `gke-idle-cluster` | GKE clusters running no nodes | **$73/month, whatever is on them** |

Three of these behave differently from the AWS equivalent people expect:

- **An idle Cloud NAT is nearly free.** Google bills NAT gateway uptime per VM
  using it, so a gateway with nothing behind it costs only the external
  addresses it holds. An AWS NAT gateway bills a flat ~$32/month regardless,
  which is why it tops every AWS waste list and does not top this one.
- **A GKE cluster costs $0.10/hour whether or not anything runs on it.**
  Scaling every node pool to zero removes the node cost and leaves the $73/month
  management fee exactly where it was.
- **A reserved static IP costs more idle than in use.** Google charges a higher
  hourly rate for an address attached to nothing, so this is one of the few
  places where the waste is more expensive than the work.

## About the numbers

Costs are estimates from on-demand **list prices**, not from your bill. They
ignore committed use discounts, sustained use discounts, private pricing and
credits.

Prices come from the Cloud Billing Catalog API, not from anyone's memory, and
are regenerated with:

```
uv run python -m zombiescan.pricing.refresh
```

The table is per-region because region moves the number: a Filestore GB is
$0.25/month zonal and $0.45 regional, and Cloud SQL SSD storage runs from
$0.17 to well over $0.40 depending on where it sits.

**Reading the catalog correctly is the subtle part.** Google fronts many SKUs
with a free allowance priced at zero — the first 30 GB of standard Persistent
Disk, the first 0.5 GB of Artifact Registry, the first six secret versions.
Taking tier 0 of those records the rate as free, which does not fail loudly: it
silently prices every finding in that section at nothing and the scan reports a
clean project. The refresher takes the first tier that actually charges, and
refuses to write a table that empties a section the previous one had.

A `~` next to a cost means it is an estimate or an upper bound — the region had
no price entry, the resource type was unrecognised, or the resource bills
incrementally rather than on provisioned size. Every finding carries a `note`
in `--json` output saying which.

## What it deliberately does not flag

Checks would rather miss waste than invent it. A false positive here costs
someone an outage.

- The newest image in a family is live even if nothing names it directly: a
  template pinned to `--image-family` resolves to it.
- A snapshot with no recorded source disk cannot be *proven* orphaned.
- Snapshots are matched by source disk **id**, not name — a disk deleted and
  recreated under the same name is a different disk.
- A subnet Google created for its own plumbing (proxy-only, Private Service
  Connect) is load-bearing.
- A spare subnet inside a network that *is* running something may be waiting
  for a workload.
- A firewall rule with no target tags applies to everything in the network.
  That is broad, not unused.
- An empty Artifact Registry repository costs nothing.
- A log bucket retaining a year is a policy; ten years is an oversight.
  `_Required` is fixed by Google at 400 days and cannot be changed at all.
- An uptime check pointed at an external URL is never judged — nothing in the
  project says whether that hostname is still meant to be up.

Two checks report **staleness**, which is a prompt to look rather than a
verdict. `stale-secret` is judged on the age of the newest version, because
Secret Manager does not report access times through this API; the finding says
so rather than claiming the secret is unread. `stale-artifact-repository` is
priced as an explicit upper bound: Artifact Registry bills each unique layer
once, and images sharing a base layer are counted once per image here.

## Cleaning up

`scan` tells you what to delete. `clean` does it.

```
zombiescan clean                          # dry run: prints the exact API calls, changes nothing
zombiescan clean --apply                  # asks before each resource
zombiescan clean --apply --yes            # no prompts
zombiescan clean --from findings.json     # act on a report you have already read
zombiescan clean --check unused-static-ip --apply
zombiescan clean --apply --audit audit.json
```

**Dry run is the default and it is exact.** `--apply` changes one thing:
whether a planned call is sent. It does not change which calls get planned, so
what the dry run shows is what the real run does.

**Backups come first where Google allows one.** Disks are snapshotted before
deletion and Cloud SQL instances given a backup run before they go. If the
backup step fails, the destructive step that assumed it does not run.

**Irreversible steps are labelled.** Deleting a disk after snapshotting it is
recoverable; deleting the snapshot is not. Releasing a static IP returns it to
Google's pool, and deleting a secret, a DNS zone, an Artifact Registry
repository or a GKE cluster destroys what is in it with no undo. Destroying a
KMS key version is *not* marked irreversible: Google holds it for 24 hours and
`gcloud kms keys versions restore` brings it back.

**Deleting a stopped VM keeps its disks.** The plan clears `autoDelete` on
every attached disk first, as its own step, so the dry run shows exactly which
disks are about to be spared. They then show up as `unattached-disk` on the
next scan, with a snapshot-first plan of their own.

**`--audit` writes a record** of every call attempted, its parameters, its
result and what it saved — the file you will want when someone asks what
happened.

Four findings have no cleaner, and say why instead of guessing:
`empty-vpc-network` (the deletion order depends on what else references what),
`idle-filestore` (the backup that would make it safe needs a region and tier
nothing chooses for you), `unbounded-log-bucket` and `unmanaged-gcs-bucket`
(both are judgements about what the project must keep).

### Permissions for cleaning

`clean --apply` needs write access, which is a different posture from scanning.
The read-only role in [`policy/`](policy/) deliberately cannot delete anything.
Grant the delete permissions to a separate principal, scoped to what you intend
to remove.

## The generated script

`--script` writes a plan. zombiescan never runs it, and has no flag that will.

Every value interpolated into a command is shell-quoted, so a resource name
cannot become a command in the file you are about to execute. Google's own
naming rules make that unreachable today, but a tool whose whole proposition is
handing you a script to run should not depend on a remote service's input
validation for local shell safety.

Every generated command carries `--project` and `--quiet`. The first means
pasting one into a shell configured for a different project deletes nothing by
surprise; the second means it cannot block on a confirmation prompt that a
script has no way to answer.

## Output formats

**`--json`** is a versioned contract, not a dump. The schema lives at
[`docs/findings.schema.json`](docs/findings.schema.json) and the suite
validates against it. Check `schema_version` before parsing: it is `3`, and
findings carry `project` and `location` where earlier versions carried
`region`.

**`--html`** is a single self-contained file — no stylesheet, no font, no
script, no network request — with a print stylesheet, because printing to PDF
from a browser is how most people will produce a PDF.

**`--script`** is the cleanup plan as bash, commented with what each command
saves and why the resource was flagged.

## Exit codes

`0` on success, including when a scan finds nothing. `1` when every
project/check pair failed — a scan that reached nothing found nothing, and
exiting `0` would tell a CI job the project was clean. `2` for a bad invocation
or missing credentials.

A check whose API is switched off on a project is **not** a failure. It is
counted under `pairs_unavailable` and skipped, because a project that has never
used Filestore has no Filestore waste.

## Permissions

[`policy/zombiescan-scanner-role.yaml`](policy/zombiescan-scanner-role.yaml) is
a custom role holding exactly the list and get permissions the checks use.
`zombiescan apis` prints the APIs behind them, and the suite fails if a check
declares an API the role does not cover.

It has not been verified under a principal actually constrained by it —
credentials with Owner or Editor are not limited the way the role describes.
Bind it to a dedicated service account and scan with that before relying on the
claim.

## Development

```
uv sync
uv run pytest                               # offline, fixtures, no credentials
ZOMBIESCAN_LIVE=1 uv run pytest -m live     # one real scan, opt-in
uv run ruff format . && uv run ruff check --fix .
make help                                   # the rest
```

Checks are tested against committed JSON fixtures of real API responses, so the
suite runs offline with no credentials. Every check has a fixture and a test
file of its own, and `tests/test_packs.py` fails if one does not.

### Packs

Checks live in packs under `src/zombiescan/packs/<pack>/`, discovered by
existing rather than listed in an import block. A pack owns its checks, its
cleaners, its price rates and the fetchers that refresh them. `core` and `gke`
are built in and load through the same path as a pack installed from PyPI, so
the seam is exercised on every run rather than only by third parties.

[`docs/PACKS.md`](docs/PACKS.md) is the pack-author contract.

**A pack is code that runs with your Google Cloud credentials**, and nothing
here verifies that its checks only read. The read-only guarantee holds for the
packs in this repository because they are reviewed. Install third-party packs
on the same judgement you would apply to any other dependency.

## License

MIT.
EOF
