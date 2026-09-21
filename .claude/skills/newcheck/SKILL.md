---
name: newcheck
description: Scaffold a new zombiescan waste check end to end - the check module, a scrubbed API fixture, its test, the pricing rate and fetcher, and its cleaner or refusal. Use when adding a new resource type to the zombie catalog.
---

Add a new waste check named `$ARGUMENTS` (e.g. `unused-nat-gateway`).

If no name was given, ask which resource type to check before doing anything.

## Steps

1. **Read `@PLAN.md`** and find the row for this check in the zombie catalog
   table. It gives the waste condition and the rough monthly cost basis. If the
   check is not in the table, add a row for it.

2. **Pick the pack.** Checks live in `src/zombiescan/packs/<pack>/`. Use
   `core` unless the resource belongs to a service that already has its own
   pack (GKE), or is a whole service nothing currently scans — in which case
   read `@docs/PACKS.md` and start a pack for it. There is no import list to
   update: a module in a pack directory is discovered by existing.

3. **Write the check module** at `src/zombiescan/packs/<pack>/<name>.py`.
   Follow the shape of the existing modules in that directory — match their
   signature, return type, and registration decorator rather than inventing a
   new one. If the check is genuinely one describe call plus one filter, build
   it with `simple_check` from `zombiescan.building`. Anything needing a
   second API call, a sum over sub-resources, or a judgement a predicate
   cannot express stays a plain `@check` function.

   Add the API to `zombiescan.gcp.API_VERSIONS` if it is not there, and name
   it in the check's `apis=` argument so `zombiescan apis` and the read-only
   role stay complete.

   Constraints, no exceptions:
   - List and get calls only. Never a mutating call.
   - Paginate with `gcp.paginate`, or cover every location at once with
     `gcp.aggregated` (Compute Engine) or a `locations/-` parent. Projects
     that accumulate zombies have a lot of them.
   - A check runs once per project. Set each finding's `location` from the
     resource itself — the aggregation scope, a `zone`/`region` field, or the
     resource path — rather than assuming one.
   - Cloud KMS and Artifact Registry reject `locations/-`; those walk
     locations with `helpers.across_locations`, which hands each worker its
     own client. Never close over a client built in the calling thread.
   - Return findings with resource id, project, location, the reason it is
     waste, the estimated monthly cost, and a remediation command as a
     *string*. Wrap interpolated ids in `helpers.arg`, and give every command
     `--project` and `--quiet`.
   - Never execute the remediation command.

4. **Record a fixture** at `tests/fixtures/<name>.json` — the raw Google API
   response shape the check consumes, keyed the way the discovery client
   returns it (`{"items": ...}` for a list, `{"items": {"zones/...": {...}}}`
   for an aggregated one). Capture it from a real call if credentials are
   live, otherwise hand-write it to match the documented API shape. Keep it
   small: enough rows to cover the waste case, the healthy case, and one edge
   case. Include a scope holding only a `warning` if the check aggregates —
   that is how Compute reports an empty zone.

5. **Write the test** at `tests/test_<name>.py`, driving the check off the
   fixture with the `make_context` fixture and no network access. Assert on
   which resources are flagged and on the computed cost, not just the count.

6. **Add the rate and its fetcher.** Register a `RateSpec` (in the pack's
   `rates.py`, or `zombiescan/pricing/rates.py` for core) and price the finding
   through `ctx.pricing.rate(...)`. Then register a `@price_fetcher` for the
   table section so `python -m zombiescan.pricing.refresh` can rebuild it —
   `tests/test_packs.py` asserts every section has exactly one fetcher, so a
   missing one fails the suite. Read the rate with `unit_price`, not tier 0:
   many SKUs open with a free allowance priced at zero, and reading that
   silently makes every finding in the section free.

7. **Give it a cleaner or a refusal.** Either add a planner to the pack's
   `cleaners.py` (back up before destroying, mark `irreversible=True` where
   there is no recovery window) or pass `uncleanable="<why not>"` to `@check`.
   A check with neither fails `test_clean.py`.

8. **Run** `uv run pytest` and `uv run ruff check src tests`, then report the
   new check's output against the fixture. `test_packs.py` also asserts every
   check has a test file of its own, so step 5 is not optional.

Do not run a live scan as part of this skill — that is `/livesmoke`.
