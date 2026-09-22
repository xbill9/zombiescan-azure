---
name: zombiescan
description: Scan Azure subscriptions for unused resources — unattached managed disks, idle NAT gateways, App Service plans hosting no apps, orphaned NICs and a dozen more — price them, and plan the cleanup. Use when asked what Azure resources are being wasted, why a bill is high, what can be deleted safely, or to explain or clean up a zombiescan finding.
---

# zombiescan

Find the Azure resources nobody is using, and what they cost.

The `zombiescan` MCP server exposes the same engine as the CLI. Every tool it
offers is read-only. Nothing in this plugin deletes an Azure resource, and
nothing in it runs a remediation command.

## The tools

| Tool | What it does |
| --- | --- |
| `list_checks` | What the scanner looks for. Needs no credentials. |
| `scan_subscription` | Scans, prices, writes a JSON report, returns the totals |
| `estimate_savings` | Totals and breakdowns over a report, with a filter |
| `explain_finding` | Why a resource counts as waste and what it would cost to keep |
| `plan_cleanup` | The exact ARM requests a cleanup would send, and which are irreversible |

## How to run a scan

1. **Scan once.** `scan_subscription` with no arguments scans the subscription
   `az` is set to. Pass `all_subscriptions: true` when the person wants
   everything they can see — the forgotten resources are in the subscription
   nobody opens, and often in the tenant nobody opens.

   **One email address is often two Azure accounts.** Microsoft lets the same
   address be both a work or school account and a personal Microsoft account,
   with separate directories and separate subscriptions. `all_subscriptions`
   covers every tenant the Azure CLI has signed into — but only those. If the
   person expects a subscription that is missing, they have not signed in to
   that account. When both share one email address they cannot share one
   Azure CLI cache (azure-cli#20168): the second account goes in its own
   config directory, `AZURE_CONFIG_DIR=~/.azure-personal az login --tenant
   <id>`, and is scanned from the CLI with the same variable set. Do not
   suggest a second `az login` into the default cache; it breaks token
   requests for both accounts.
2. **Keep the `report_path` it returns.** Every other tool takes it. Scanning
   again to answer a follow-up costs API calls and produces different numbers
   than the ones already quoted.
3. **Lead with the monthly total and the two or three checks behind most of
   it**, from the `by_check` breakdown. A list of 200 resource ids is not an
   answer.

A scan is one pass per subscription, not one per region: an ARM list call
covers every resource group and region at once, and each finding carries the
region and the resource group it lives in.

## Ask the tools for arithmetic

`estimate_savings` answers every how-much, how-many and which-is-biggest
question, with a filter by check, location, subscription, resource group,
resource type or cost. Use it instead of adding up findings from the report. It
returns the exact total, count, cheapest and costliest, grouped by each of
those — and it echoes the filter it applied, under `filter_applied`. Read that
echo: a filter naming a check that is not in the report returns a precise zero,
and `no_such_checks_in_report` is what tells you the filter was wrong rather
than the subscription clean.

## What to be careful about

- **Costs are list-price estimates**, computed from a bundled price table, not
  from the subscription's bill. They exclude reservations, savings plans, Azure
  Hybrid Benefit, dev/test rates and enterprise agreement pricing. Say so when
  quoting a total.
- **`complete: false` in a scan result is not an all-clear.** It means every
  subscription/check pair failed. Zero findings there means nothing was
  scanned.
- **`pairs_unavailable` is the number that decides how much a clean result is
  worth.** A check is skipped where its resource provider is not registered on
  the subscription, which is ordinary — but if 12 of 22 pairs were skipped, the
  scan found no waste in the ten services that were actually looked at, not in
  Azure. Say which of the two you mean. If a service the person expected is
  missing from the results, this is the first thing to check.
- **A `~` cost, or `approximate_cost: true`, is an upper bound or a fallback
  region's price.** `explain_finding` says which.
- **Never run a remediation command** from a finding, and never run
  `zombiescan clean --apply`. Those are the operator's to run.

## Two findings that surprise people arriving from other clouds

- **An idle NAT gateway is expensive here.** Azure bills it a flat hourly fee
  — about $32.85 a month — whether or not a subnet is attached, the way an AWS
  NAT gateway does and unlike Google's Cloud NAT.
- **An idle AKS cluster may be free.** Only the Standard and Premium tiers pay
  for a control plane; a Free-tier cluster scaled to zero costs nothing, which
  is the reverse of GKE. The finding prices each cluster at its own tier.

## Cleaning up

`plan_cleanup` shows what would happen: the requests in order, the backup
before the destruction where the API allows one, and every step with no undo
flagged `irreversible`. It plans and stops. Show the plan, name the
irreversible steps out loud, and hand over the command for the person to run
themselves:

```
zombiescan clean --from <report path> --apply
```

It is a dry run without `--apply`, prompts per resource without `--yes`, and
refuses a report produced by different credentials.

Azure has more recovery windows than most clouds, and `irreversible` marks the
real ones. A deleted Key Vault key is recoverable for the vault's soft-delete
period and a deleted SQL database restores from its backups, so neither is
flagged; a released public IP address is gone for good, and that one is.

Some findings have no cleaner. `plan_cleanup` returns them under `unsupported`
with the reason — report the reason rather than improvising a delete command.

## When the credentials fail

The tools name the fix in the error, and the message is `az`'s own — pass it on
rather than reporting it as a scan failure. `az login` is the answer when a
token has expired, and only then: a subscription that is not found is a
different problem, and the error says which it is.
