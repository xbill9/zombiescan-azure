# zombiescan — build plan

**Scope decision:** local only. No hosted site, no publish endpoint. The scan
runs on the local machine against the local Google Cloud credentials.

## What it is

An open-source, local-first Google Cloud waste scanner. It runs on your machine
with your existing Application Default Credentials, finds resources you are
paying for that nothing is using, prices them, and prints a report with a
dollar total.

Positioning: not "here are your 14 Persistent Disks" but "9 of these are
attached to nothing and cost you $47/month; here is the plan to kill them."

Two front doors, one engine:

1. **CLI** — `zombiescan scan --all-projects`
2. **Claude Code plugin** — a skill plus an MCP server, so the agent can run a
   scan, explain a finding, and draft the cleanup itself

Nothing leaves the machine. No service account key handover, no findings
uploaded anywhere. That is the whole privacy story and it is a real advantage
over every SaaS tool in this space.

## The unit of fan-out is the project

This is the one structural difference from an AWS scanner, and it shapes
everything above the client layer.

Almost every AWS list call is regional, so an AWS scanner must fan out across
seventeen regions and multiply every check by that. Google Cloud publishes two
things that remove the need:

- **`aggregatedList`** on Compute Engine returns disks, addresses, instances,
  routers, subnetworks and forwarding rules across every zone and region in one
  call.
- **`locations/-`**, a wildcard accepted by Filestore, Cloud Logging, GKE and
  others, means every location.

So a check runs once per project and reads each finding's location off the
resource. `Finding.location` holds a zone, a region or `global`; `Finding.project`
is the account-equivalent, because a resource id is unique inside a project and
nowhere else.

Two exceptions, both verified against the live API: **Cloud KMS** and
**Artifact Registry** reject `locations/-`. Those checks enumerate the API's
locations and walk them in parallel through `helpers.across_locations`.

## The zombie catalog

Each check returns: resource id, project, location, why it is considered waste,
estimated monthly cost, and a suggested remediation command. The commands are
printed; `clean` is a separate command that runs them.

### core pack

| Check | Why it's waste | Rough monthly cost |
| --- | --- | --- |
| ✅ Unattached Persistent Disks | Billed in full while attached to nothing | $0.04–0.17/GB by type |
| ✅ Unused static IPs | Billed *higher* when idle than when in use | ~$7.30 each |
| ✅ Stopped instances | The VM is free, its disks are not | disk cost |
| ✅ Orphaned snapshots | Source disk gone | ~$0.05/GB |
| ✅ Unused custom images | Nothing boots from them | ~$0.05/GB |
| ✅ Idle Cloud NAT | Gateway in a network with no VMs | its reserved IPs only |
| ✅ Idle forwarding rules | Load balancer with no backends | ~$18/month |
| ✅ Unused subnets | IP range reserved against nothing | $0 (blocks reuse) |
| ✅ Unused firewall rules | Disabled, or targeting a tag nothing carries | $0 (hygiene) |
| ✅ Empty VPC networks | Nothing running inside | the priced waste within |
| ✅ Stopped Cloud SQL instances | Stopped, but storage still bills | storage, ×2 if regional |
| ✅ Unused Cloud DNS zones | Only the SOA and NS records | $0.20 at the first tier |
| ✅ Stale secrets | No new version in 90 days | $0.06 per version per replica |
| ✅ Disabled KMS key versions | Disabling does not stop the charge | $0.06 software, $1.00 HSM |
| ✅ Idle Filestore | In a network with no compute to mount it | $0.25–0.45/GB |
| ✅ Stale Artifact Registry repos | No push in 90 days | $0.10/GB (upper bound) |
| ✅ Unbounded log buckets | Retention never expires | unpriced; reported as growth |
| ✅ Unmanaged GCS buckets | Versioning on, no lifecycle rule | unpriced; reported as growth |
| ✅ Unused uptime checks | Monitoring a deleted VM | $0 (the alerts are the cost) |

### gke pack

| Check | Why it's waste | Rough monthly cost |
| --- | --- | --- |
| ✅ Idle GKE clusters | Management fee charged whatever runs on it | **$73/month** |

GKE is a separate pack as the proof the seam carries a whole service: its own
API (`container.googleapis.com`), its own rate section, its own fetcher against
a SKU family core never looks at.

**Three findings behave differently from the AWS instinct**, and the checks say
so rather than leaving the reader to assume:

- **An idle Cloud NAT is nearly free.** Google bills gateway uptime per VM
  using it. An AWS NAT gateway bills a flat hourly charge regardless, which is
  why it tops every AWS waste list and does not top this one. What an idle
  Cloud NAT does cost is the external addresses it holds.
- **A GKE cluster bills $0.10/hour whatever is on it.** Scaling every node pool
  to zero removes the node cost and leaves the management fee untouched.
- **A reserved static IP costs more idle than attached.** One of the few places
  where the waste is more expensive than the work.

## Pricing

Prices come from the **Cloud Billing Catalog API**
(`cloudbilling.googleapis.com`), read with ADC, and are bundled as
`table.json` so a scan works offline and adds no latency.

The catalog is not organised by product the way the console is: a SKU carries a
category (resource family, group, usage type), a free-text description, the
regions it applies to, and a tiered price. Nothing in it says "pd-balanced". So
each fetcher states the exact family, group and description it matches, and a
matcher that stops matching yields an empty section — which `main` refuses to
write over a populated one.

Two traps, both of which fail silently and both of which cost a rebuild to
find:

- **Tier 0 is often a free allowance.** The first 30 GB of standard Persistent
  Disk, the first 0.5 GB of Artifact Registry, the first six secret versions,
  the first 5 GB of Cloud Storage are all priced at zero. Reading tier 0
  records the rate as free and prices every finding in the section at nothing.
  `unit_price()` takes the first tier that charges.
- **Some SKUs are published against the region `global`.** Cloud NAT addresses,
  Artifact Registry storage and log retention have no per-region entry at all,
  and a per-region fetcher returns an empty section for them.

Verified rates in us-central1 at the time of writing: pd-standard $0.04/GB,
pd-balanced $0.10, pd-ssd $0.17, snapshots and images $0.05, GCS Standard
$0.020, idle static IP $0.010/hour, forwarding rule minimum $0.025/hour, DNS
zone $0.20 (tiered to $0.10 past 25), secret version $0.06, KMS software key
version $0.06 and HSM $1.00, Artifact Registry $0.10/GB, GKE cluster
$0.10/hour.

**IAM posture:** read-only. `policy/zombiescan-scanner-role.yaml` is a custom
role holding exactly the list and get permissions the checks use, generated
from the `apis=` each check declares and kept in step by the suite.

## Architecture

```
your laptop
-----------
Application Default Credentials (gcloud auth application-default login)
        |  google.auth.default(), refreshed once up front and locked thereafter
        v
zombiescan engine (python, google-api-python-client)
  multi-project, parallel, read-only
  gcp.py        discovery clients (thread-local), pagination, locations
  packs/        one module per zombie check, + cleaners.py
  pricing/      bundled price table, rate registry, refresh script
  engine.py     project fan-out, credential handling
  report.py     terminal table, JSON, remediation script
  cli.py        click entry point
        |
        +--> terminal report (rich table, dollar total)
        +--> findings.json  (schema_version 3)
        +--> report.html    (self-contained, print-to-PDF)
        +--> cleanup.sh     (printed, never executed)
        |
        v
Claude Code plugin
  skill + slash commands + MCP server
  tools: list_checks, scan_project, estimate_savings, explain_finding, plan_cleanup
```

**Repo layout**

```
zombiescan-gcp/
  src/zombiescan/
    gcp.py        discovery clients, pagination, location parsing
    packs/        pack manifest, discovery, API version
      core/         one module per zombie check, + cleaners.py
      gke/          checks, cleaners, rates.py, refresh.py
    pricing/      bundled price table, rate registry, refresh script
    building.py   simple_check, for checks that are one call and one filter
    helpers.py    shared helpers, public to packs
    engine.py     project fan-out, credential handling
    report.py     terminal table, JSON, remediation script
    html.py       self-contained HTML report
    clean.py      the plan runner
    cli.py        click entry point
  docs/PACKS.md   the pack-author contract
  plugin/         Claude Code plugin: skill, slash commands, MCP server
  policy/         minimal read-only custom role
  tests/          check logic against recorded fixtures
```

### One thing that must not regress

The client layer crashed the interpreter twice during the build, both times
with SIGSEGV or a glibc abort rather than an exception, and both causes are
easy to reintroduce:

1. **Concurrent `credentials.refresh()`.** Every worker thread finds no token
   at the start of a scan and refreshes at once; the refresh signs through
   OpenSSL via cffi and corrupts the heap. `gcp.make_thread_safe` fetches the
   first token single-threaded and locks later refreshes.
2. **A discovery client shared between threads.** Its `httplib2` connection is
   not safe to share. `Clients` keeps them thread-local, and
   `helpers.across_locations` passes each worker its own rather than letting a
   callback close over one built in the calling thread.

Both are documented where they live. Neither is covered by the offline suite,
because neither reproduces without real concurrency against a real endpoint.

## Distribution

MIT on GitHub, installable with `uv tool install git+...` or `pipx`, plus the
Claude Code plugin in the same repo.
`.claude-plugin/marketplace.json` at the repo root makes it installable with
`/plugin marketplace add xbill9/zombiescan-gcp`.

## Wanted but not built

- **Metric-driven checks** — idle Cloud SQL by connection count, over-provisioned
  Bigtable, KMS keys unused per audit log. Every check so far answers from a
  single list call; these need Cloud Monitoring and a lookback window, which is
  a new capability rather than another row.
- **Org-level scanning.** `--all-projects` uses Resource Manager's
  `projects.search`, which covers what the caller can see. Walking a folder
  hierarchy deliberately, with per-folder totals, is a different shape.
- **Committed use discount awareness.** Every figure here is list price. A
  project with a CUD is overcharged by this report, and saying by how much
  needs the billing export rather than the catalog.

## Open questions

- Cost lookback for "idle" judgements needs Cloud Monitoring metrics; decide
  the default window (7 days is the usual answer).
- Whether `--all-projects` should default on. Off is faster and safer; on is
  what people actually want, because the forgotten resources are always in the
  project nobody opens.
