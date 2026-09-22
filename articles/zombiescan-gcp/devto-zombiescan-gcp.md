---
title: "What Nobody Is Using in Your Google Cloud Projects, and What It Costs"
published: false
description: "A local-first CLI that scans Google Cloud projects for unused resources, prices each one from the Cloud Billing Catalog API, and drafts the cleanup. One call per project instead of one per region, read-only by default, and a Claude Code plugin over the same engine."
tags: googlecloud, python, devops, opensource
cover_image: https://raw.githubusercontent.com/xbill9/zombiescan-gcp/main/articles/zombiescan-gcp/devto-cover.4e999e75.jpg
---

This article provides a step by step guide to building a Google Cloud waste scanner from source, running it across every project your credentials can see, pricing each finding from the Cloud Billing Catalog API, and drafting the cleanup. A suite of Python checks is built to cover Compute Engine, Cloud SQL, Storage, DNS, KMS, Secret Manager, Artifact Registry, Filestore, Cloud Logging, Cloud Monitoring and GKE.

https://github.com/xbill9/zombiescan-gcp

---

#### What Gets Left Behind

A Persistent Disk survives the instance it was attached to. A static IP outlives the migration that freed it. A GKE cluster keeps charging its management fee after the last node pool scaled to zero. Each one bills every hour and reports nothing.

The billing console shows the total. The Recommender surfaces candidates. A figure that drives a decision names one resource, in one project, and its monthly cost.

`zombiescan` produces that figure for 20 classes of resource across two packs.

---

#### At This Point You Should Have…

- A Google Cloud account and Application Default Credentials, from `gcloud auth application-default login`
- `uv` on the path, and Python 3.11 or newer
- Read access to the projects you mean to scan — the predefined `roles/viewer` covers every call, and `zombiescan-scanner-role.yaml`, under `policy/` in the repository, is a 16-permission custom role that covers exactly as much

---

#### Step 1 — Build It From Source

```shell
git clone https://github.com/xbill9/zombiescan-gcp
cd zombiescan-gcp
uv sync
```

```plaintext
Resolved 39 packages in 0.58ms
Checked 38 packages in 0.19ms
```

`uv sync` reads the committed `uv.lock`, so the resolved set is the one the tests ran against.

```shell
uv run zombiescan --version
```

```plaintext
zombiescan, version 0.1.0
```

---

#### Step 2 — Install It as a Tool

Working from the clone keeps `uv run` in front of every command. Installing it puts `zombiescan` on the path instead.

```shell
uv tool install git+https://github.com/xbill9/zombiescan-gcp
```

Both routes run the same engine. The rest of this article uses the installed form.

---

#### Step 3 — Authenticate

```shell
gcloud auth application-default login
```

`google.auth.default()` picks that up. No key file is downloaded, and no service account is created.

Every worker thread in a scan shares one credentials object, and the first token is fetched before the threads start. Refreshing credentials signs through OpenSSL, and one credentials object refreshed from several threads at once ends the process instead of raising, so the wrapper serialises the refreshes behind a lock.

---

#### Step 4 — List the Checks

```shell
zombiescan checks
```

```plaintext
  disabled-kms-key  Disabled KMS key versions still billing  (core)
  empty-vpc-network  VPC networks with nothing running in them  (core)
  gke-idle-cluster  GKE clusters running no nodes  (gke)
  idle-cloud-nat  Cloud NAT gateways serving no instances  (core)
  idle-filestore  Filestore instances nothing can mount  (core)
  idle-forwarding-rule  Load balancers with no backends  (core)
  orphaned-snapshot  Snapshots of deleted disks  (core)
  stale-artifact-repository  Artifact Registry repositories with no recent pushes  (core)
  stale-secret  Secrets nothing has updated in 90 days  (core)
  stopped-instance  Stopped instances still paying for disks  (core)
  stopped-sql-instance  Stopped Cloud SQL instances still paying for storage  (core)
  unattached-disk  Unattached Persistent Disks  (core)
  unbounded-log-bucket  Log buckets retaining logs indefinitely  (core)
  unmanaged-gcs-bucket  Versioned buckets with no lifecycle rule  (core)
  unused-dns-zone  Cloud DNS zones publishing nothing  (core)
  unused-firewall-rule  Firewall rules matching nothing  (core)
  unused-image  Custom images nothing boots from  (core)
  unused-static-ip  Unused static IP addresses  (core)
  unused-subnet  Subnets reserving ranges nothing uses  (core)
  unused-uptime-check  Uptime checks monitoring deleted resources  (core)
```

Nineteen ship in the `core` pack and one in `gke`. GKE sits in its own pack because it carries its own API, its own rate section and its own price fetcher, which is what a third-party pack has to supply.

---

#### Step 5 — Which APIs the Checks Need

```shell
zombiescan apis
```

```plaintext
  artifactregistry.googleapis.com  1 check(s)
  cloudkms.googleapis.com  1 check(s)
  compute.googleapis.com  12 check(s)
  container.googleapis.com  1 check(s)
  dns.googleapis.com  1 check(s)
  file.googleapis.com  1 check(s)
  logging.googleapis.com  1 check(s)
  monitoring.googleapis.com  1 check(s)
  secretmanager.googleapis.com  1 check(s)
  sqladmin.googleapis.com  1 check(s)
  storage.googleapis.com  1 check(s)

An API that is not enabled on a project is skipped, not reported as an error: a
project that never used a service has no waste in it.
```

Each check declares the APIs it calls. That declaration generates this list and the read-only custom role, and the test suite fails when a check names an API the role does not cover.

---

#### Step 6 — Scan One Project

```shell
zombiescan scan --project glitnir-dev
```

```plaintext
Scanning as application default credentials
1 project(s), 20 check(s) — read-only

zombiescan — 2 findings across 1 project

Check                      Found  Monthly
stale-artifact-repository      1    $0.01
empty-vpc-network              1    $0.00
```

Every call is a list or a get. The scan has no code path that deletes, modifies or releases anything.

---

#### Step 7 — Scan Every Project

```shell
zombiescan scan --all-projects
```

```plaintext
Scanning as application default credentials
75 project(s), 20 check(s) — read-only

zombiescan — 713 findings across 75 projects

Check                      Found  Monthly
unattached-disk               36  $153.53
unused-static-ip              12   $89.06
unused-image                  64   $75.25
stopped-instance              12   $35.76
orphaned-snapshot             39   $28.66
stale-artifact-repository     13    $4.75
stale-secret                   9    $0.54
unused-dns-zone                1    $0.20
empty-vpc-network             13    $0.00
unmanaged-gcs-bucket         118    $0.00
unused-firewall-rule          43    $0.00
unused-subnet                349    $0.00
unused-uptime-check            4    $0.00
```

```plaintext
Estimated waste: $387.74/month ($4,652.87/year)
```

`--all-projects` asks Resource Manager's `projects.search` which projects the caller can see, then fans out over them. 75 projects times 20 checks is 1,500 project/check pairs, and 411 of them were skipped because the API was not enabled on that project. The sweep took 79 seconds.

Thirteen of the 20 checks fired. Five of those thirteen classes carry no charge and account for 527 of the 713 findings: empty VPC networks, versioned buckets with no lifecycle rule, firewall rules matching nothing, unused subnets and uptime checks monitoring deleted resources. They are reported because they accumulate without limit and block deletions.

---

#### Step 8 — Read the Per-Resource Table

The detail table sorts the costliest findings first and guarantees a row to every check that fired.

```plaintext
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Project        ┃ Location                ┃ Resource               ┃  Monthly ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ comglitn       │ global                  │ w5-opt                 │   $53.19 │
│ glitnir-ba1    │ europe-west1-b          │ disk-20260326-160533   │   $50.00 │
│ glitnir-ba1    │ europe-west1-c          │ disk-20260326-152217   │   $50.00 │
│ glitnir-mb1    │ us-east1-c              │ mb1                    │   $21.76 │
│ comglitn       │ us-central1-c           │ disk-opt-w1            │   $20.00 │
│ xbill-b1       │ us                      │ snapshot-b1-east       │   $16.57 │
│ comglitn       │ us-central1-c           │ w2                     │   $10.88 │
│ glitnir-sx1    │ us-east4                │ sx2                    │    $8.03 │
│ swoonbox1      │ us-east4                │ swoonbox-landing-4     │    $8.03 │
│ comglitn       │ us-central1             │ w1                     │    $7.30 │
└────────────────┴─────────────────────────┴────────────────────────┴──────────┘
```

Project is the first column because a resource id is unique inside a project and nowhere else. Location holds a zone, a region or `global`, read off each resource.

A `~` marks an estimate or an upper bound, and the per-finding `note` in JSON output names the reason.

🔎 Tip: `--min-cost 5` hides findings under five dollars a month, `--limit 0` prints every row instead of the top 25, and `--location us-central1` keeps a region's zones along with the region itself.

---

#### Step 9 — Where a Check Runs

A check runs once per project. Two properties of Google's APIs make that a complete sweep.

Compute Engine's `aggregatedList` returns disks, addresses, instances, routers, subnetworks and forwarding rules across every zone and region in one call. Most other APIs accept `locations/-`, a wildcard meaning every location.

Cloud KMS and Artifact Registry reject `locations/-`. Those two enumerate the API's locations and walk them in parallel, and each worker gets its own client, because the HTTP connection underneath a discovery client cannot be shared between threads.

---

#### Step 10 — Where the Prices Come From

The bundled price table is generated from the Cloud Billing Catalog API.

```shell
python -m zombiescan.pricing.refresh
```

A scan reads `table.json` from the installed package, so it runs offline and adds no latency. Rates are looked up by key and region: `ctx.pricing.rate("disk.gb_month", region=...)`.

Verified rates in us-central1, from the bundled table:

| Rate | us-central1 |
|---|---|
| pd-standard | $0.04/GB/month |
| pd-balanced | $0.10/GB/month |
| pd-ssd | $0.17/GB/month |
| Snapshots and custom images | $0.05/GB/month |
| Idle static IP | $0.010/hour |
| Forwarding rule minimum | $0.025/hour |
| GKE cluster | $0.10/hour |
| Secret version | $0.06/month |
| KMS software key version | $0.06/month |
| KMS HSM key version | $1.00/month |
| Artifact Registry storage | $0.10/GB/month |

At 730 hours to the month, arithmetic over those hourly rates gives $7.30 a month for an idle static IP, $18.25 for a forwarding rule, and $73.00 for a GKE cluster with no nodes on it.

---

#### Step 11 — Read the First Tier That Charges

Google fronts many SKUs with a free allowance priced at zero: the first 30 GB of standard Persistent Disk, the first 0.5 GB of Artifact Registry, the first six secret versions. A fetcher that reads tier 0 records the rate as free, which prices every finding in that section at nothing and reports a clean project.

`unit_price()` takes the first tier that charges. Reading a named tier deliberately — the way the Cloud DNS fetcher walks the zone tiers at $0.20 for the first 25 and $0.10 beyond them — is a separate call.

A SKU published against the region `global` has no per-region entry at all. Cloud NAT addresses, Artifact Registry storage and log retention are all published that way, so they live in a global section with a `scope="global"` rate spec.

The refresher refuses to write a table that loses or empties a section the previous one had, because an empty section means a matcher stopped matching.

---

#### Step 12 — JSON Output

```shell
zombiescan scan --project glitnir-dev --json findings.json
```

```json
{
  "schema_version": 3,
  "scan": {
    "generated": "2026-09-21T19:55:55Z",
    "duration_seconds": 5.41,
    "principal": "application default credentials",
    "projects": ["glitnir-dev"],
    "pairs_attempted": 20,
    "pairs_unavailable": 3,
    "complete": true
  },
  "pricing": {
    "generated": "2026-09-21T16:35:39Z",
    "basis": "Google Cloud Billing Catalog API, on-demand USD list prices",
    "excludes": ["committed use discounts", "sustained use discounts", "private pricing", "credits"]
  },
  "totals": {
    "monthly_cost": 0.01,
    "annual_cost": 0.12,
    "finding_count": 2
  }
}
```

Each finding carries its own reason, cost, remediation command and a `details` block:

```json
{
  "check": "stale-artifact-repository",
  "resource_id": "gcr.io",
  "project": "glitnir-dev",
  "location": "us",
  "reason": "DOCKER repository holding 0.1 GB with nothing pushed to it in 282 days",
  "monthly_cost": 0.01,
  "approximate_cost": true,
  "remediation": "gcloud artifacts repositories delete gcr.io --location=us --project=glitnir-dev --quiet",
  "details": {
    "stored_gb": 0.1,
    "idle_days": 282,
    "usd_per_gb_month": 0.1,
    "note": "upper bound: Artifact Registry bills each unique layer once, and images sharing a base layer are counted once per image here"
  }
}
```

The pricing block dates the table and lists what list prices exclude, so a report read six months later says which rates produced it.

---

#### Step 13 — The HTML Report

```shell
zombiescan scan --project glitnir-dev --html report.html
```

One self-contained file with the styles inline, which prints to PDF without fetching anything.

---

#### Step 14 — The Cleanup Plan

```shell
zombiescan scan --project glitnir-dev --script cleanup.sh
```

```shell
#!/usr/bin/env bash
# zombiescan cleanup plan — generated 2026-09-21T19:55:55Z
#
# READ EVERY LINE BEFORE RUNNING THIS.
# zombiescan generated this file and did not run it. Deleting Google
# Cloud resources is not reversible. Where a backup is possible the
# command takes one first, but a backup is not a substitute for
# knowing what you are deleting.
#
# 2 resource(s), about $0.01/month.

set -euo pipefail

# glitnir-dev us gcr.io — DOCKER repository holding 0.1 GB with nothing pushed to it in 282 days
# saves about $0.01/month
gcloud artifacts repositories delete gcr.io --location=us --project=glitnir-dev --quiet

# glitnir-dev global default — VPC network runs no instances; it still holds none. This is the auto-created default network
# saves about $0.00/month
gcloud compute networks delete default --project=glitnir-dev --quiet
```

Every generated command carries `--project` and `--quiet`, and interpolated resource ids are shell-quoted. The test suite checks all three at the source, because a remediation command is printed for an operator to run.

---

#### Step 15 — Cleaning Up

`clean` is a separate command, and a dry run is what it does with no flags.

```shell
zombiescan clean --project glitnir-dev
```

```plaintext
Scanning as application default credentials — 1 project(s)

dry run — 1 of 2 finding(s) can be cleaned, 1 cannot
  skip empty-vpc-network default: a network cannot be deleted until everything
inside it is gone -- subnets, routes, firewall rules, Cloud Routers, peerings
and any Private Service Connect attachment -- and the order depends on what else
references them. Clean the findings inside the network first; this one goes away
with them

stale-artifact-repository  gcr.io  glitnir-dev · us · $0.01/mo
  DOCKER repository holding 0.1 GB with nothing pushed to it in 282 days
    → IRREVERSIBLE delete Artifact Registry repository gcr.io and its images
      artifactregistry.projects.locations.repositories.delete({'name':
'projects/glitnir-dev/locations/us/repositories/gcr.io'})

Would free about $0.01/month across 1 resource(s); 1 include irreversible steps.
Nothing was changed. Re-run with --apply to perform these.
```

`--apply` gates one thing: whether a planned step is sent to Google Cloud. It never changes which steps get planned, so the preview above is what runs.

Planning is read-only. A cleaner may read — fetching an instance's disks to clear `autoDelete`, reading a router's NAT list — and it yields the mutations as objects for the runner to send.

Where the API allows a backup, the backup step is ordered before the destruction, and a failed step stops the rest of that finding. A failed snapshot can never be followed by the delete that assumed it.

`IRREVERSIBLE` marks a step with no recovery window. A released static IP is gone, so it carries the mark; a destroyed KMS key version is held for 24 hours, so it does not.

A check with no cleaner reports the finding as unsupported with a reason, which is the `skip` line above. The test suite fails a check that has neither a cleaner nor a reason.

🔎 Tip: `clean --from findings.json` acts on a report already reviewed, and refuses a report produced by different credentials.

---

#### Step 16 — The Read-Only Role

```shell
gcloud iam roles create zombiescanScanner --project=PROJECT_ID \
  --file=policy/zombiescan-scanner-role.yaml --quiet
```

Sixteen permissions, every one a list or a get. The file is generated from the `apis=` each check declares, and the suite keeps it in step.

`clean` needs more than this, deliberately: the identity that reports waste should be unable to delete what it reports.

---

#### Step 17 — The Claude Code Plugin

```shell
/plugin marketplace add xbill9/zombiescan-gcp
```

The plugin ships two slash commands, a skill and an MCP server over the same engine.

```plaintext
  scan_project  Scan Google Cloud projects for unused resources and price them
  estimate_savings  Total, count and break down the findings in a report
  explain_finding  Explain why a check treats a resource as waste
  list_checks  List the installed checks
  plan_cleanup  Show exactly what `zombiescan clean` would do
```

Every tool is read-only. `plan_cleanup` builds the step objects and stops there; the function that sends them to Google Cloud is unreachable from the server, and a test asserts the module names no other `clean.*` attribute.

The tools return computed figures — totals, counts, breakdowns, cheapest and costliest — and echo the filter they applied. A tool that returned rows for the model to add up would move the arithmetic to the place least able to do it.

The protocol is JSON-RPC over stdio in the standard library, with no MCP SDK dependency for about a hundred lines of framing.

---

#### Three Findings That Differ From the AWS Instinct

| Resource | Google Cloud behaviour |
|---|---|
| Idle Cloud NAT | Gateway uptime bills per VM using it, so a gateway serving nothing costs the addresses it holds — $3.65 a month each at $0.005/hour. An AWS NAT gateway bills a flat hourly charge whatever uses it. |
| GKE cluster | $0.10/hour whatever runs on it. Scaling every node pool to zero removes the node cost and leaves the $73.00 a month management fee. |
| Reserved static IP | Google bills an idle static IP at a higher hourly rate than one attached to a running instance, so the waste costs more than the work. |

---

#### What the Checks Leave Alone

A false positive here costs an outage, so each check states the condition it declines to act on.

| Condition | Treatment |
|---|---|
| Load balancer with backends that are unhealthy | An outage; left alone |
| Snapshot whose source disk still exists | Load-bearing; left alone |
| Disk attached to a stopped instance | Reported under `stopped-instance`, counted once |
| Google-managed KMS key, disabled | Free; never reported |
| Key or secret already scheduled for destruction | Already on a timer; left alone |
| Default VPC network with nothing in it | Reported, and flagged as the auto-created default |
| An API not enabled on a project | Skipped; a service never used holds no waste |

`empty-vpc-network` reports without a cleanup step. A network deletes only after its subnets, routes, firewall rules, Cloud Routers and peerings are gone, in an order that depends on what references them, so the finding says so where a command would otherwise appear.

Four of the 75 projects returned `permission denied: no access` on every check, producing 42 skipped project/check pairs. Those appear in the report as a list, and the monthly total covers the projects that were readable.

---

#### Compare and Contrast

| | zombiescan | Recommender / Active Assist | Billing reports | Cross-project SaaS |
|---|---|---|---|---|
| Per-resource dollar figure | 🥇 yes | partial | aggregate only | yes |
| Credentials leave the machine | 🥇 never | n/a, Google-side | n/a, Google-side | ❌ service account granted |
| Deletes on request | 🥈 opt-in, dry run first | ❌ no | ❌ no | 🥇 yes |
| Price source | Billing Catalog API, dated | Google-side | your bill | vendor |
| Runs offline after install | 🥇 yes | ❌ no | ❌ no | ❌ no |
| Breadth | 20 checks | broader | n/a | broader |

---

#### So, Which One?

Billing reports answer what the projects spent. Recommender covers more ground and reaches areas outside these 20 checks, including the metric-driven judgements that need a lookback window.

`zombiescan` fits the case where the answer has to be per-resource, priced, and produced without granting anything access to the projects. A laptop, existing credentials, and about 80 seconds for 75 projects.

---

#### Cost

A scan costs nothing. List and get calls carry no charge, and the price table ships with the package, so a scan makes no Billing Catalog call.

Regenerating the table calls the Cloud Billing Catalog API, which is also free.

---

#### Tests

```shell
uv run pytest -q
```

```plaintext
324 passed, 4 deselected in 0.37s
```

The suite runs offline against recorded API responses, with no credentials. The four deselected tests reach a real project and run under `ZOMBIESCAN_LIVE=1`.

Recorded responses are keyed the way the discovery client returns them, and every aggregated one includes a scope holding only a `warning`, which is how Compute Engine reports an empty zone. A check that walks past that shape without skipping it fails here instead of in production.

---

#### Teardown

```shell
uv tool uninstall zombiescan-gcp
```

Nothing is left in any project. The tool creates no service account, no role, no bucket and no stored state. Revoke the local credentials with `gcloud auth application-default revoke`.

---

#### Summary

The goal of this article was to audit every Google Cloud project a set of credentials can reach and attach a monthly cost to each unused resource. The key to the solution was fanning out per project instead of per region, and fetching every rate from the Cloud Billing Catalog API while keeping the scan read-only. The results were:

- 🟢 20 checks in two packs across Compute Engine, Cloud SQL, Storage, DNS, KMS, Secret Manager, Artifact Registry, Filestore, Cloud Logging, Cloud Monitoring and GKE
- 🟢 75 projects swept in 79 seconds, 713 findings, $387.74 a month and $4,652.87 a year
- 🟢 One call per project: `aggregatedList` covers every zone and region, and `locations/-` covers every location for the APIs that accept it
- 🟢 Prices dated in every report, with the first charging tier read instead of a free tier 0
- 🟢 JSON against a published schema, a self-contained HTML report, and a shell cleanup plan that is printed and never run
- 🟢 `clean` previews by default, orders a backup before the destruction it protects, and marks steps with no recovery window
- ⚠️ 411 of 1,500 project/check pairs were skipped because the API was not enabled, and 42 more for lack of access to 4 projects, so $387.74 covers the projects that were readable
- ⚠️ Figures are on-demand list prices, excluding committed use discounts, sustained use discounts, private pricing and credits
- ⚠️ Artifact Registry is an upper bound: it bills a shared layer once, and a finding counts it once per image
- ❌ `empty-vpc-network` reports without a cleanup step, because a network needs its dependencies removed in an order that depends on what references them
- ❌ Idle judgements that need a lookback window — Cloud SQL by connection count, KMS keys by audit log — need Cloud Monitoring and are outside these 20 checks

Scope: one set of Application Default Credentials, 75 projects reachable through `projects.search`, 20 checks, a single sweep per figure quoted, run from one laptop. The read-only guarantee holds for the packs in this repository, which are reviewed; a pack installed from PyPI runs with the same credentials and carries no such guarantee.

The strategy for using per-resource pricing for Google Cloud waste detection was validated with an incremental step by step approach.

---

#### References

- Repository, MIT: https://github.com/xbill9/zombiescan-gcp
- Findings JSON Schema: https://github.com/xbill9/zombiescan-gcp/blob/main/docs/findings.schema.json
- Pack author guide: https://github.com/xbill9/zombiescan-gcp/blob/main/docs/PACKS.md
- Cloud Billing Catalog API, `services.skus.list`: https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list
- Compute Engine `aggregatedList`: https://cloud.google.com/compute/docs/reference/rest/v1/disks/aggregatedList
- VPC pricing, external IP addresses: https://cloud.google.com/vpc/network-pricing
- GKE cluster management fee: https://cloud.google.com/kubernetes-engine/pricing
- Application Default Credentials: https://cloud.google.com/docs/authentication/application-default-credentials
