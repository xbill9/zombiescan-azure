---
description: Scan Google Cloud projects for unused resources and report what they cost.
argument-hint: "[project ...] or --all-projects"
---

Scan the Google Cloud projects these credentials can reach and report the waste.

Projects requested: $ARGUMENTS (empty means the project gcloud is configured for).

1. Call `scan_project` on the `zombiescan` MCP server. Pass `projects` if any
   were named above; pass `all_projects: true` if the arguments say so.
2. Report, in this order:
   - the monthly total and the annual figure beside it
   - the checks behind most of it, from `by_check`
   - the costliest few resources, with their project and location
   - anything under `errors`, and the `warning` if the scan was incomplete
3. Say that the figures are list-price estimates rather than the project's
   bill, and that nothing was changed.
4. Give the `report_path` so follow-up questions can use it.

Answer follow-ups with `estimate_savings` against that report rather than
scanning again or adding the findings up yourself.

A check whose API is not enabled on a project is skipped rather than failed —
`pairs_unavailable` counts those. If a service the operator expected is missing
from the results, that is where to look.
