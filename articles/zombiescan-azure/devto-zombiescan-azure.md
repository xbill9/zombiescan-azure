---
title: "What Nobody Is Using in Your Azure Subscriptions, and What It Costs"
published: false
description: "A local-first CLI that scans Azure subscriptions for unused resources, prices each one from the Azure Retail Prices API, and drafts the cleanup. One call per subscription across every tenant you are signed into, read-only by default, and a Claude Code plugin over the same engine."
tags: azure, python, devops, opensource
cover_image: https://raw.githubusercontent.com/xbill9/zombiescan-azure/main/articles/zombiescan-azure/devto-cover.298b6dcc.jpg
---

This article provides a step by step guide to building an Azure waste scanner from source, running it across every subscription your `az login` can see, pricing each finding from the Azure Retail Prices API, and drafting the cleanup. A suite of Python checks is built to cover Compute, Networking, Storage, SQL, Key Vault, Container Registry, App Service, Container Apps, AI Services, Machine Learning, Log Analytics and AKS.

https://github.com/xbill9/zombiescan-azure

---

#### The Third Cloud

This is the third scanner in a series. The first covered an AWS account and the second covered Google Cloud projects:

- AWS: https://dev.to/aws-builders/find-the-aws-resources-nobody-is-using-and-what-they-cost-you-35bj
- Google Cloud: https://dev.to/gde/what-nobody-is-using-in-your-google-cloud-projects-and-what-it-costs-1k0

The shape carries over: a CLI, a bundled price table, a cleanup plan that is printed and never run, and a Claude Code plugin with an MCP server over the same engine. What changes is everything underneath — how Azure authenticates, how it answers a list call, how it prices a disk, and which resources keep billing once nobody uses them.

---

#### What Gets Left Behind

A managed disk survives the VM it was attached to. A public IP outlives the load balancer that held it. A NAT gateway keeps its hourly fee after the last subnet moved away, and a provisioned model deployment bills every PTU every hour whether a request arrives or not. Each one reports nothing.

Cost Management shows the total. Azure Advisor surfaces candidates. A figure that drives a decision names one resource, in one subscription and resource group, and its monthly cost.

`zombiescan` produces that figure for 30 classes of resource across two packs.

---

#### At This Point You Should Have…

- An Azure account and the Azure CLI, signed in with `az login`
- `uv` on the path, and Python 3.11 or newer
- Read access to the subscriptions you mean to scan — the built-in Reader role covers every call, and `zombiescan-scanner-role.json`, under `policy/` in the repository, is a custom role holding exactly the read actions the checks use

---

#### Step 1 — Build It From Source

```shell
git clone https://github.com/xbill9/zombiescan-azure
cd zombiescan-azure
uv sync
```

```plaintext
Resolved 18 packages in 0.48ms
Checked 17 packages in 0.13ms
```

`uv sync` reads the committed `uv.lock`, so the resolved set is the one the tests ran against. The dependency list is `click` and `rich`; every Azure call goes over HTTPS with `urllib` from the standard library.

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
uv tool install git+https://github.com/xbill9/zombiescan-azure
```

Both routes run the same engine. The rest of this article uses the installed form.

---

#### Step 3 — Authenticate, and See Which Account You Are

```shell
az login
```

`zombiescan` shells out to `az account get-access-token` and reuses the result. No key file is downloaded, no client secret is stored, and there is no `azure-identity` dependency.

An ARM token is issued for exactly one tenant. Microsoft lets one email address be both a work or school account and a personal Microsoft account, and those are two directories with two sets of subscriptions. So the scanner reads the subscription list from `az account list --all`, which spans every identity the CLI has signed into, and holds one token per tenant.

Every scan opens by naming each tenant, the kind of account signed into it, and each subscription:

```plaintext
Scanning as xbill@glitnir.com
Tenant Default Directory — personal Microsoft account
  domain        xbillglitnircom.onmicrosoft.com
  tenant id     40482c55-d00d-4c6d-8903-643d76a74b9c
  signed in as  xbill@glitnir.com
  subscription  Azure subscription 1 (default)
                3db3ce66-50b6-4d11-91ef-5950cf4039ed
```

`az account list` reports a personal account and a work account as the same user with the same email. The access token tells them apart: a personal Microsoft account signs in through `live.com` and its token carries `idp: live.com`, a work or school account in its own directory carries no `idp` at all, and a guest from another directory names its home issuer there.

🔎 Tip: every token is fetched on the main thread before any worker starts. Concurrent `az account get-access-token` processes contend on the MSAL token cache in `~/.azure`, and a corrupted cache costs an `az login`.

---

#### Step 4 — List the Checks

```shell
zombiescan checks
```

```plaintext
  aks-idle-cluster  AKS clusters running no nodes  (aks)
  deallocated-vm  Stopped VMs still paying for disks  (core)
  disabled-key-vault-key  Disabled Key Vault keys still billed  (core)
  empty-ai-services-account  AI Services accounts with no model deployed  (core)
  empty-container-apps-environment  Container Apps environments with no app  (core)
  empty-container-registry  Container registries holding no images  (core)
  empty-resource-group  Resource groups holding nothing  (core)
  empty-vnet  Virtual networks with nothing running in them  (core)
  idle-app-service-plan  App Service plans hosting no apps  (core)
  idle-container-app  Always-on container apps serving nothing  (core)
  idle-dedicated-host  Dedicated hosts running no VMs  (core)
  idle-load-balancer  Load balancers with no backends  (core)
  idle-ml-compute  ML compute running or held idle  (core)
  idle-nat-gateway  NAT gateways with no subnets  (core)
  idle-provisioned-deployment  Provisioned model deployments serving nothing  (core)
  idle-workload-profile  Dedicated workload profiles running no app  (core)
  orphaned-nic  Network interfaces with no VM  (core)
  orphaned-snapshot  Snapshots of deleted disks  (core)
  paused-sql-database  Paused SQL databases still paying for storage  (core)
  stale-key-vault-secret  Secrets with no new version in 90 days  (core)
  unattached-disk  Unattached managed disks  (core)
  unbounded-log-workspace  Log workspaces with no ingestion cap  (core)
  unmanaged-storage-account  Versioned storage accounts with no lifecycle policy  (core)
  unused-availability-test  Availability tests watching a deleted resource  (core)
  unused-capacity-reservation  Capacity reservations holding unused slots  (core)
  unused-dns-zone  DNS zones with no records  (core)
  unused-image  Managed images nothing boots from  (core)
  unused-nsg  Security groups protecting nothing  (core)
  unused-public-ip  Public IP addresses attached to nothing  (core)
  unused-subnet  Subnets reserving a range against nothing  (core)
```

Twenty-nine ship in the `core` pack and one in `aks`. AKS sits in its own pack because it carries its own resource provider, its own rate section and its own price fetcher, which is what a third-party pack has to supply.

---

#### Step 5 — Which Resource Providers the Checks Read

```shell
zombiescan providers
```

```plaintext
  Microsoft.App  3 check(s)
  Microsoft.CognitiveServices  2 check(s)
  Microsoft.Compute  6 check(s)
  Microsoft.ContainerRegistry  1 check(s)
  Microsoft.ContainerService  1 check(s)
  Microsoft.Insights  3 check(s)
  Microsoft.KeyVault  2 check(s)
  Microsoft.MachineLearningServices  1 check(s)
  Microsoft.Network  9 check(s)
  Microsoft.OperationalInsights  1 check(s)
  Microsoft.Resources  1 check(s)
  Microsoft.Sql  1 check(s)
  Microsoft.Storage  1 check(s)
  Microsoft.Web  1 check(s)
```

This list decides more on Azure than on the other two clouds. **ARM answers a list call against an unregistered resource provider with HTTP 200 and an empty page.** A subscription that has never used App Service returns `{"value": []}` from `/providers/Microsoft.Web/serverfarms`, with no error. A check that simply ran would find nothing and report a clean subscription.

So every check declares the providers it reads, the engine reads each subscription's registrations once, and a check whose provider is missing is counted as skipped. The same declaration generates the read-only custom role, and the test suite fails when a check names a provider the role does not cover.

---

#### Step 6 — Scan One Subscription

```shell
zombiescan scan
```

```plaintext
1 subscription(s), 30 check(s) — read-only

No waste found across 1 subscription. Nothing to clean up.

4 of 30 subscription/check pair(s) were skipped: their resource provider is not registered, so there
is nothing of that kind here. 'zombiescan providers' lists what each check reads.
```

With no flag it scans the subscription `az` is set to. The four skipped pairs are Key Vault's two checks, App Service and SQL: this subscription has never registered those providers, so it has no resources of those kinds.

Every call is a list, a get or a Resource Graph query. The scan has no code path that deletes, modifies or releases anything.

---

#### Step 7 — Scan Every Subscription, in Every Tenant

```shell
zombiescan scan --all-subscriptions
```

`--all-subscriptions` takes every enabled subscription in `az account list --all`, across every tenant the CLI has signed into, and runs `az account list --refresh` first to pick up subscriptions created since the last login. On this account that is one subscription in one tenant, and the sweep of 30 checks took 8.65 seconds.

A second identity is one `az login` away. After it, the same command reaches both directories, requests for each subscription carry that tenant's token, and the header lists both tenants with their account kinds.

---

#### Step 8 — What a Finding Looks Like

This subscription is clean, so the rows below come from the test suite's recorded ARM responses — the same JSON each check is tested against — priced from the real bundled price table.

```shell
uv run python articles/zombiescan-azure/show-fixture-findings.py
```

```plaintext
49 findings from 30 checks, recorded responses, prices generated 2026-09-23T17:19:30Z
total $32,345.26/month

Check                              Resource                           Monthly
idle-provisioned-deployment        acct-ptu/ptu-idle              $21,900.00
idle-ml-compute                    ws-research/gpu-warm            $4,467.60
idle-dedicated-host                host-idle                       $3,084.25
idle-workload-profile              env-single/d4-solo                $522.62
idle-app-service-plan              prod-plan                         $459.90
unused-capacity-reservation        cr-partial                        $420.48
empty-container-apps-environment   env-empty-dedicated               $297.81
idle-workload-profile              env-mixed/d4-idle                 $224.81
idle-ml-compute                    ws-research/ci-forever            $213.89
unused-capacity-reservation        cr-idle                           $140.16
aks-idle-cluster                   scaled-to-zero                     $73.00
idle-nat-gateway                   egress-gw-old                      $32.85
deallocated-vm                     batch-runner                       $24.90
idle-load-balancer                 api-lb                             $21.90
unattached-disk                    orphan-data                        $19.71
idle-container-app                 app-idle                           $11.83
orphaned-nic                       web-01-nic-old                      $3.65
unattached-disk                    tiny-scratch                        $0.60
```

The top of that list is where Azure's waste concentrates now: a 15-PTU regional model deployment serving nothing is $21,900 a month, a GPU cluster whose minimum keeps two idle nodes up is $4,467.60, and an empty `DSv3-Type3` dedicated host is $3,084.25. A forgotten disk is $19.71.

Every finding carries its subscription, resource group, location and full ARM id. No `az` command works without the resource group, and the ARM id is the only identifier unique across a tenant.

---

#### Step 9 — Where a Check Runs

A check runs once per subscription. Two properties of ARM make that a complete sweep.

A list call at `/subscriptions/<id>/providers/Microsoft.Compute/disks` returns every disk in every resource group and every region, in one call. And Azure Resource Graph answers a cross-type join in one KQL query, so the check for stopped VMs reads power state for every VM at once without an instance-view call per machine.

So `@check` has no region scope. Each finding reads its own `location` off the resource, and `--location` filters findings afterwards.

🔎 Tip: follow `nextLink` even when the first page is empty. Listing AI Services accounts across a subscription returned an empty first page with a `nextLink`, and the account on the second page. A reader that stopped at page one would report the subscription clean.

---

#### Step 10 — Pinned API Versions

Azure has no "latest". Every request carries a required `api-version`, and `azure.API_VERSIONS` pins one per resource type, resolved by longest prefix so a sub-type inherits its parent's version.

A retired version produces `InvalidResourceType`, which is 404-shaped, and a 404 reads as "nothing here". The check would stop reporting and the subscription would look that much cleaner. The live test, gated behind `ZOMBIESCAN_LIVE=1`, sends every pin to ARM and fails on the first one ARM rejects.

---

#### Step 11 — Where the Prices Come From

The bundled price table is generated from the Azure Retail Prices API, which is public: no credentials and no subscription.

```shell
python -m zombiescan.pricing.refresh
```

A scan reads `table.json` from the installed package, so it runs offline and adds no latency. Rates are looked up by key and region: `ctx.pricing.rate("disk.tier_month", region=..., variant="P10 LRS")`.

Verified rates in eastus, from the bundled table:

| Rate | eastus |
|---|---|
| Standard static public IP | $0.005/hour |
| NAT Gateway | $0.045/hour |
| Standard Load Balancer rules | $0.025/hour |
| AKS Standard control plane | $0.10/hour |
| Dedicated host `DSv3-Type3` | $4.225/hour |
| `Standard_D2s_v3`, Linux | $0.096/hour |
| Provisioned throughput, regional | $2.00 per PTU-hour |
| Provisioned throughput, global | $1.00 per PTU-hour |
| Container Apps Dedicated management fee | $0.10/hour |

At 730 hours to the month, arithmetic over those hourly rates gives $3.65 for an idle public IP, $32.85 for a NAT gateway, $18.25 for load balancer rules, $73.00 for an AKS Standard control plane and $3,084.25 for an empty dedicated host.

---

#### Step 12 — Four Ways a Price Goes Wrong

Each of these produces a plausible table with the wrong numbers in it.

**Filter on `priceType eq 'Consumption'`.** The same meter is published as `Reservation` and `DevTestConsumption` too, at a fraction of the price.

**Take the first tier that charges.** Azure returns a tiered meter's rows in no guaranteed order, and tier 0 is often a free allowance. `unit_price()` sorts by `tierMinimumUnits` and reads the first nonzero row.

**Match the meter name as well as the SKU.** `P80 LRS Disk`, `P80 LRS Disk Mount` and `P80 LRS Disk Operations` all share the SKU "P80 LRS"; only the first is the capacity charge. The same care covers Windows rows: most VM products spell it "Windows", and a few GPU series abbreviate it to "Win".

**Some meters have no ARM region.** NAT Gateway and Load Balancer are published against "Global", and Azure DNS against a billing geography spelled "Zone 1". Those live in a global section.

The refresher refuses to write a table that loses or empties a section the previous one had, because an empty section means a matcher stopped matching.

---

#### Step 13 — Managed Disks Are Priced by Tier

A managed disk bills at the tier its provisioned size falls into, flat per month:

| Disk | Tier | eastus per month |
|---|---|---|
| 1 GiB Premium SSD | P1 | $0.60 |
| 65 GiB Premium SSD | P10 | $19.71 |
| 128 GiB Premium SSD | P10 | $19.71 |
| 4 GiB Standard HDD | S4 | $1.54 |
| 32 GiB Standard HDD | S4 | $1.54 |

A 65 GiB Premium disk and a 128 GiB one cost the same. Standard HDD has no rung below S4, so a 4 GiB Standard disk bills as a 32 GiB one. The price table is keyed by tier and redundancy — "P10 LRS", "P10 ZRS" — because zone redundancy costs about half as much again: $29.57 for a P10 ZRS.

Premium SSD v2 and Ultra bill per provisioned GiB, and both bill provisioned IOPS and throughput on top, which the finding says.

---

#### Step 14 — Idle Needs a Week of Metrics

Most checks answer from one list call. Two need usage:

- `idle-provisioned-deployment` reads `ModelRequests` on each account over seven days, split by `ModelDeploymentName`, so one call covers every deployment on the account
- `idle-container-app` reads `Requests` on each app with `minReplicas` of one or more

Both go through `helpers.metric_totals`, which asks Azure Monitor for the `Total` over `P7D` and returns the sum. A deployment that served nothing has no series at all, and a missing series reads as zero. Both checks declare `Microsoft.Insights`, the provider behind the metrics endpoint, so the registration check covers it too.

On this subscription the container app recorded 0 requests in the seven days, and the gpt-5-mini deployment returned no series. Both stay out of the report: the app has `minReplicas: 0` and the deployment is pay-per-token, so each costs nothing idle.

---

#### Step 15 — JSON Output

```shell
zombiescan scan --all-subscriptions --json findings.json
```

```json
{
  "schema_version": 4,
  "scan": {
    "generated": "2026-09-23T17:47:01Z",
    "duration_seconds": 8.65,
    "principal": "xbill@glitnir.com",
    "subscriptions": ["3db3ce66-50b6-4d11-91ef-5950cf4039ed"],
    "pairs_attempted": 30,
    "pairs_unavailable": 4,
    "complete": true
  },
  "pricing": {
    "generated": "2026-09-23T17:19:30Z",
    "basis": "Azure Retail Prices API, pay-as-you-go USD list prices",
    "excludes": ["reservations", "savings plans", "Azure Hybrid Benefit", "dev/test rates", "enterprise agreement pricing", "credits"]
  },
  "totals": {
    "monthly_cost": 0,
    "annual_cost": 0,
    "finding_count": 0,
    "by_check": {}
  }
}
```

The pricing block dates the table and lists what list prices exclude, so a report read six months later says which rates produced it. `pairs_unavailable` carries the skipped count, and `complete` is false when every pair failed or was skipped, so a CI job reading the file can tell an all-clear from a scan that reached nothing.

---

#### Step 16 — The HTML Report

```shell
zombiescan scan --all-subscriptions --html report.html
```

One self-contained file with the styles inline, which prints to PDF without fetching anything. With no findings its headline reads "No waste found" and names how many subscription/check pairs ran, how many were skipped for an unregistered provider, and how many failed.

---

#### Step 17 — The Cleanup Plan

```shell
zombiescan scan --all-subscriptions --script cleanup.sh
```

```shell
#!/usr/bin/env bash
# zombiescan cleanup plan — generated 2026-09-23T17:47:01Z
#
# READ EVERY LINE BEFORE RUNNING THIS.
# zombiescan generated this file and did not run it. Some of these
# deletions can be undone and some cannot: Key Vault and SQL keep a
# recovery window, a released public IP address is gone for good.
```

Every generated command is built by one helper, which adds `--resource-group` and `--subscription` and wraps interpolated names in shell quoting. The test suite checks both at the source.

🔎 Tip: `--yes` exists only on the `az` commands that would otherwise prompt, and passing it to one that does not is an error. `az disk delete` takes it; `az snapshot delete` and `az network nic delete` reject it. The helper holds the list of commands that take it, and the live test re-checks that list against the installed CLI.

---

#### Step 18 — Cleaning Up

`clean` is a separate command, and a dry run is what it does with no flags.

```shell
zombiescan clean --all-subscriptions
```

```plaintext
Nothing to clean.
```

`--apply` gates one thing: whether a planned step is sent to Azure. It never changes which steps get planned, so the preview is what runs.

Planning is read-only. A cleaner may read — re-listing a resource group to confirm it is still empty, re-reading a model deployment's request count, re-reading a capacity reservation's allocation — and it yields the mutations as objects for the runner to send. A plan refuses when the resource has changed since the scan: a host with a VM placed on it, an account that gained a deployment, a reservation that filled up.

Where the API allows a backup, it comes first. The disk cleaner takes an incremental snapshot before the delete, and a failed step stops the rest of that finding.

`IRREVERSIBLE` marks a step with no recovery window at all. A released public IP is gone, and so is a deleted Container Apps environment's static IP, so both carry the mark. A Key Vault key is held by mandatory soft-delete, a SQL database restores from point-in-time backups, and a deleted AI Services account is recoverable for 48 hours, so none of those does.

A check with no cleaner reports its findings as unsupported with a reason. `idle-container-app` is one: lowering `minReplicas` trades the idle charge for a cold start on the next request, and that trade belongs to whoever owns the app.

---

#### Step 19 — The Read-Only Role

```shell
az role definition create --role-definition policy/zombiescan-scanner-role.json
```

Forty-four actions: forty-three reads and Container Registry's `listUsages`, the one management-plane call that reports how much a registry stores. The file is generated from the `providers=` each check declares, and the suite keeps it in step.

`clean` needs more than this, deliberately: the identity that reports waste should be unable to delete what it reports.

---

#### Step 20 — The Claude Code Plugin

```shell
/plugin marketplace add xbill9/zombiescan-azure
```

The plugin ships two slash commands, a skill and an MCP server over the same engine.

```plaintext
  scan_subscription  Scan Azure subscriptions for unused resources and price them
  estimate_savings  Total, count and break down the findings in a report
  explain_finding  Explain why a check treats a resource as waste
  list_checks  List the installed checks
  plan_cleanup  Show exactly what `zombiescan clean` would do
```

Every tool is read-only. `plan_cleanup` builds the step objects and stops there; the function that sends them to Azure is unreachable from the server, and a test asserts the module names no other `clean.*` attribute.

The tools return computed figures — totals, counts, breakdowns, cheapest and costliest — and echo the filter they applied. A tool that returned rows for the model to add up would move the arithmetic to the place least able to do it.

---

#### Step 21 — What a Scan Cannot See

A clean scan answers one question: is anything billing that nothing uses? It says nothing about a project that has gone quiet as a whole. This subscription holds one resource group, `research-mesh-rg`, with six resources: a container app and its environment, a Foundry account with one project and a gpt-5-mini deployment, a container registry and a Log Analytics workspace.

Cost Management reports what the group was charged, and the totals below are Azure's own:

| Resource | Last 30 days | Since Aug 13 |
|---|---|---|
| `researchmeshacr`, Container Registry Basic | $5.08 | $6.87 |
| `research-mesh-foundry`, gpt-5-mini tokens | $0.008 | $0.25 |
| Log Analytics workspace | $0.00 | $0.00 |
| Container app and environment | no charges | no charges |

$5.09 over the last 30 days, and the registry is 99.8% of it. Its Basic tier bills a flat daily fee whether anyone pulls an image or not. Everything else bills on use, and use is close to zero: the app served 0 requests in seven days and the deployment under a cent of tokens in thirty.

None of the six trips a check, because each is in use by another: the registry holds the image the app runs, the account has a deployment, and the environment has an app. Finding a group like this takes Cost Management data, which is a different source from anything a scan reads.

---

#### Three Clouds, Same Resource

The same idle resource costs differently on each cloud. Figures are from each article's bundled price table.

| Resource | AWS (us-east-1) | Google Cloud (us-central1) | Azure (eastus) |
|---|---|---|---|
| Idle NAT gateway | 🥈 $32.85/month flat | 🥇 the addresses it holds, $3.65 each | 🥈 $32.85/month flat |
| 128 GB disk, attached to nothing | 🥇 $10.24, gp3 per GB | 🥈 $12.80, pd-balanced per GB | 🥉 $19.71, P10 by tier |
| Managed Kubernetes, no nodes | — | 🥈 $73.00 management fee | 🥇 $0.00 on Free, $73.00 on Standard |
| Unit a check fans out over | region | project | subscription |
| Price source | AWS Price List API | Cloud Billing Catalog API | Azure Retail Prices API, no credentials |

Two checks exist only on Azure. **Orphaned NICs**, because a network interface is a resource of its own that outlives its VM and blocks deleting the public IP, subnet and virtual network it references. And **empty resource groups**, because a resource group is a container inside a subscription.

🔎 Tip: an Azure public IP bills the same rate attached or idle, so nothing in the price signals that it is unused. On Google Cloud an idle static IP costs more than one in use.

---

#### What the Checks Leave Alone

A false positive here costs an outage, so each check states the condition it declines to act on.

| Condition | Treatment |
|---|---|
| Public IP held by an orphaned NIC, a deallocated VM or an idle load balancer | Priced on the holder's finding, counted once |
| Disk reserved by a stopped VM | Reported under `deallocated-vm`, counted once |
| Disk in the `ActiveSAS` state | Something is reading it; left alone |
| Snapshot whose source disk still exists | Load-bearing; left alone |
| AI Services account with a project but no deployment | A project can use models hosted elsewhere; left alone |
| Container app with no ingress | A worker processes queues, so a zero request count proves nothing; left alone |
| Resource provider not registered | Skipped and counted; a service never used holds no waste |

---

#### Compare and Contrast

| | zombiescan | Azure Advisor | Cost Management | Cross-subscription SaaS |
|---|---|---|---|---|
| Per-resource dollar figure | 🥇 yes | partial | per resource, after the fact | yes |
| Credentials leave the machine | 🥇 never | n/a, Azure-side | n/a, Azure-side | ❌ service principal granted |
| Deletes on request | 🥈 opt-in, dry run first | ❌ no | ❌ no | 🥇 yes |
| Runs offline after install | 🥇 yes | ❌ no | ❌ no | ❌ no |
| Breadth | 30 checks | broader | everything billed | broader |

---

#### So, Which One?

Cost Management answers what a subscription spent, down to the resource, and it is the only source that sees a quiet project like `research-mesh-rg`. Advisor covers more ground, including right-sizing judgements this tool leaves alone.

`zombiescan` fits the case where the answer has to be per-resource, priced, and produced without granting anything access to the subscriptions. A laptop, an existing `az login`, and under ten seconds for this subscription.

---

#### Cost

A scan costs nothing. ARM list and get calls, Resource Graph queries and Azure Monitor metric reads carry no charge, and the price table ships with the package.

Regenerating the table calls the Azure Retail Prices API, which is public and free.

---

#### Tests

```shell
uv run pytest -q
```

```plaintext
380 passed, 10 deselected in 0.34s
```

The suite runs offline against recorded ARM responses, with no credentials. The ten deselected tests reach a real subscription and run under `ZOMBIESCAN_LIVE=1`: they verify every pinned `api-version` against ARM and every `--yes` command against the installed CLI, two facts about the outside world that a comment cannot keep true.

Recorded responses are keyed by the tail of the request path, and they include resources that are in use, because half of what these checks do is decline to report.

---

#### Teardown

```shell
uv tool uninstall zombiescan-azure
```

Nothing is left in any subscription. The tool creates no service principal, no role assignment, no storage account and no stored state. Sign the CLI out with `az logout`.

---

#### Summary

The goal of this article was to audit every Azure subscription a set of `az` credentials can reach and attach a monthly cost to each unused resource. The key to the solution was holding one token per tenant, checking provider registration before trusting an empty page, and pricing every finding from the Azure Retail Prices API while keeping the scan read-only. The results were:

- 🟢 30 checks in two packs across Compute, Networking, Storage, SQL, Key Vault, Container Registry, App Service, Container Apps, AI Services, Machine Learning, Log Analytics and AKS
- 🟢 Every tenant the CLI has signed into, from one `az login` each, with the account kind — personal or work — named in the header
- 🟢 One call per subscription: ARM lists are subscription-wide and Resource Graph joins resource types in one query
- 🟢 Unregistered providers skipped and counted, so an empty page never reads as a clean subscription
- 🟢 Disks priced by tier and redundancy, and the priciest classes — provisioned model throughput, GPU clusters, dedicated hosts — priced from their own meters
- 🟢 JSON against a published schema, a self-contained HTML report, and a shell cleanup plan that is printed and never run
- 🟢 `clean` previews by default, re-reads each resource before planning, and marks steps with no recovery window
- ⚠️ The subscription scanned here is clean: 0 findings, with 4 of 30 pairs skipped for unregistered providers, so the per-resource figures come from the recorded test responses
- ⚠️ Figures are pay-as-you-go list prices, excluding reservations, savings plans, Azure Hybrid Benefit and credits
- ⚠️ Two checks judge idleness from seven days of Azure Monitor metrics, which is one window among several reasonable ones
- ❌ A resource group that has gone quiet as a whole trips no check; seeing it takes Cost Management data

Scope: one personal Microsoft account with one tenant and one subscription, 30 checks, a single sweep per figure quoted, run from one laptop. Rates are eastus from a table generated 2026-09-23; the AWS and Google Cloud figures come from those articles' own price tables. The read-only guarantee holds for the packs in this repository, which are reviewed; a pack installed from PyPI runs with the same credentials and carries no such guarantee.

The strategy for using per-resource pricing for Azure waste detection was validated with an incremental step by step approach.

---

#### References

- Repository, MIT: https://github.com/xbill9/zombiescan-azure
- Findings JSON Schema: https://github.com/xbill9/zombiescan-azure/blob/main/docs/findings.schema.json
- Pack author guide: https://github.com/xbill9/zombiescan-azure/blob/main/docs/PACKS.md
- Part one, AWS: https://dev.to/aws-builders/find-the-aws-resources-nobody-is-using-and-what-they-cost-you-35bj
- Part two, Google Cloud: https://dev.to/gde/what-nobody-is-using-in-your-google-cloud-projects-and-what-it-costs-1k0
- Azure Retail Prices API: https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices
- Azure Resource Graph: https://learn.microsoft.com/en-us/azure/governance/resource-graph/overview
- Resource providers and registration: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-providers-and-types
- Managed disk pricing: https://azure.microsoft.com/en-us/pricing/details/managed-disks/
- Capacity reservation billing: https://learn.microsoft.com/en-us/azure/virtual-machines/capacity-reservation-overview
- Dedicated host pricing: https://learn.microsoft.com/en-us/azure/virtual-machines/dedicated-hosts
