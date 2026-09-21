---
name: zombiescan
description: Scan Google Cloud projects for unused resources — unattached Persistent Disks, idle GKE clusters, reserved static IPs, orphaned snapshots and a dozen more — price them, and plan the cleanup. Use when asked what Google Cloud resources are being wasted, why a bill is high, what can be deleted safely, or to explain or clean up a zombiescan finding.
---

# zombiescan

Find the Google Cloud resources nobody is using, and what they cost.

The `zombiescan` MCP server exposes the same engine as the CLI. Every tool it
offers is read-only: list and get calls only. Nothing in this plugin deletes a
Google Cloud resource, and nothing in it runs a remediation command.

## The tools

| Tool | What it does |
| --- | --- |
| `list_checks` | What the scanner looks for. Needs no credentials. |
| `scan_project` | Scans, prices, writes a JSON report, returns the totals |
| `estimate_savings` | Totals and breakdowns over a report, with a filter |
| `explain_finding` | Why a resource counts as waste and what it would cost to keep |
| `plan_cleanup` | The exact API calls a cleanup would make, and which are irreversible |

## How to run a scan

1. **Scan once.** `scan_project` with no arguments scans the project gcloud is
   configured for. Pass `all_projects: true` when the person wants everything
   they can see — the forgotten resources are in the project nobody opens.
2. **Keep the `report_path` it returns.** Every other tool takes it. Scanning
   again to answer a follow-up costs API calls and produces different numbers
   than the ones already quoted.
3. **Lead with the monthly total and the two or three checks behind most of
   it**, from the `by_check` breakdown. A list of 200 resource ids is not an
   answer.

A scan is one pass per project, not one per region: Compute Engine's
aggregated listing and the `locations/-` wildcard already cover every zone and
region, and each finding carries the exact location it lives in.

## Ask the tools for arithmetic

`estimate_savings` answers every how-much, how-many and which-is-biggest
question, with a filter by check, location, project, resource type or cost. Use
it instead of adding up findings from the report. It returns the exact total,
count, cheapest and costliest, grouped by check, by location, by project and by
resource type — and it echoes the filter it applied, under `filter_applied`.
Read that echo: a filter naming a check that is not in the report returns a
precise zero, and `no_such_checks_in_report` is what tells you the filter was
wrong rather than the project clean.

## What to be careful about

- **Costs are list-price estimates**, computed from a bundled price table, not
  from the project's bill. They exclude committed use discounts, sustained use
  discounts, private pricing and credits. Say so when quoting a total.
- **`complete: false` in a scan result is not an all-clear.** It means every
  project/check pair failed. Zero findings there means nothing was scanned.
- **A check whose API is switched off on a project is skipped, not failed.**
  `pairs_unavailable` counts those. If a service the person expected is missing
  from the results, that is the first thing to check.
- **A `~` cost, or `approximate_cost: true`, is an upper bound or a fallback
  region's price.** `explain_finding` says which.
- **Never run a remediation command** from a finding, and never run
  `zombiescan clean --apply`. Those are the operator's to run.

## Cleaning up

`plan_cleanup` shows what would happen: the calls in order, the backup before
the destruction where the API allows one, and every step with no undo flagged
`irreversible`. It plans and stops. Show the plan, name the irreversible steps
out loud, and hand over the command for the person to run themselves:

```
zombiescan clean --from <report path> --apply
```

It is a dry run without `--apply`, prompts per resource without `--yes`, and
refuses a report produced by different credentials.

Some findings have no cleaner. `plan_cleanup` returns them under `unsupported`
with the reason — report the reason rather than improvising a delete command.

## When the credentials fail

The tools name the fix in the error. Application Default Credentials expire,
and `gcloud auth application-default login` is the answer. Pass that on rather
than reporting it as a scan failure.
