---
description: Scan Azure subscriptions for unused resources and report what they cost.
argument-hint: "[subscription ...] or --all-subscriptions"
---

Scan the Azure subscriptions these credentials can reach and report the waste.

Subscriptions requested: $ARGUMENTS (empty means the one `az` is set to).

1. Call `scan_subscription` on the `zombiescan` MCP server. Pass
   `subscriptions` if any were named above; pass `all_subscriptions: true` if
   the arguments say so. That sweeps every tenant the Azure CLI has signed
   into, which is what matters when one email address is both a work account
   and a personal one.
2. Report, in this order:
   - the monthly total and the annual figure beside it
   - the checks behind most of it, from `by_check`
   - the costliest few resources, with their resource group and region
   - anything under `errors`, and the `warning` if the scan was incomplete
3. Say that the figures are list-price estimates rather than the
   subscription's bill, and that nothing was changed.
4. Give the `report_path` so follow-up questions can use it.

Answer follow-ups with `estimate_savings` against that report rather than
scanning again or adding the findings up yourself.

A check whose resource provider is not registered on a subscription is skipped
rather than failed — `pairs_unavailable` counts those. Report that count
alongside the total: "no waste found" and "no waste found in the ten services
this subscription actually uses" are different statements, and only one of them
is what happened.
