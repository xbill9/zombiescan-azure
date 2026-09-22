---
name: newcheck
description: Scaffold a new zombiescan waste check end to end - the check module, a scrubbed ARM fixture, its test, the pricing rate and fetcher, and its cleaner or refusal. Use when adding a new resource type to the zombie catalog.
---

Add a new waste check named `$ARGUMENTS` (e.g. `idle-application-gateway`).

If no name was given, ask which resource type to check before doing anything.

## Steps

1. **Read `@PLAN.md`** and find the row for this check in the zombie catalog
   table. It gives the waste condition and the rough monthly cost basis. If the
   check is not in the table, add a row for it.

2. **Pick the pack.** Checks live in `src/zombiescan/packs/<pack>/`. Use
   `core` unless the resource belongs to a service that already has its own
   pack (AKS), or is a whole service nothing currently scans — in which case
   read `@docs/PACKS.md` and start a pack for it. There is no import list to
   update: a module in a pack directory is discovered by existing.

3. **Pin the api-version.** Add the resource type to
   `zombiescan.azure.API_VERSIONS`, and get the value from the provider rather
   than from memory:

   ```
   az provider show -n Microsoft.Network \
     --query "resourceTypes[?resourceType=='applicationGateways'].apiVersions[]" -o tsv \
     | grep -v preview | sort -r | head -1
   ```

   A retired version produces `InvalidResourceType`, which is 404-shaped, so
   the engine counts the check as *unavailable* rather than raising it. The
   check silently stops running and the subscription looks cleaner.

4. **Write the check module** at `src/zombiescan/packs/<pack>/<name>.py`.
   Follow the shape of the existing modules in that directory — match their
   signature, return type, and registration decorator rather than inventing a
   new one. If the check is genuinely one list call plus one filter, build it
   with `simple_check` from `zombiescan.building`. Anything needing a second
   call, a sum over sub-resources, or a judgement a predicate cannot express
   stays a plain `@check` function.

   Constraints, no exceptions:
   - Read calls only. Never a mutating call.
   - **Declare the provider** in `providers=`. This is load-bearing: ARM
     answers a list call against an unregistered provider with HTTP 200 and an
     empty page, so a check whose provider is missing would report the
     subscription clean. The engine refuses to run it instead, and
     `zombiescan providers` and the read-only role in `policy/` come from the
     same declaration. Add the matching read action to
     `policy/zombiescan-scanner-role.json` or the suite fails.
   - A check runs once per subscription. `ctx.list(TYPE)` covers every
     resource group and region in one call; use `ctx.graph(kql)` when the
     check needs to join two resource types. Set each finding's `location`
     from the resource itself, and `global` where it genuinely has none.
   - Read nested state through `helpers.properties(resource)`: ARM nests it
     under `properties` and a projected Resource Graph row does not.
   - Compare ARM ids lowercased — `helpers.arm_ids` does it — because ARM and
     Resource Graph disagree on case and a literal comparison finds nothing
     and reports every resource as unused.
   - Return findings with the resource name, subscription, **resource group**,
     the full **`arm_id`**, location, the reason it is waste, the estimated
     monthly cost, and a remediation command as a *string*. Build the command
     with `helpers.az` and wrap interpolated ids in `helpers.arg`. Do not
     append `--yes` by hand: `helpers.az` adds it only where the command takes
     one, and `az` rejects it on the commands that do not prompt.
   - Never execute the remediation command.

5. **Record a fixture** at `tests/fixtures/<name>.json` — the ARM response
   shape the check consumes, keyed the way the client returns it
   (`{"value": [...]}` for a list, a plain list of projected rows for a
   Resource Graph query). Capture it from a real call if credentials are live,
   otherwise hand-write it to match the documented API shape. Keep it small:
   enough rows to cover the waste case, **the healthy case that must not be
   reported**, and one edge case. Scrub subscription ids and tenant ids.

6. **Write the test** at `tests/test_<name>.py`, driving the check off the
   fixture with the `make_context` fixture and no network access. Assert on
   which resources are flagged, on which are *not*, and on the computed cost —
   not just the count. Fake responses are keyed by the tail of a request path,
   so a sub-resource is keyed `/keys` or `/databases` rather than by its type.

7. **Add the rate and its fetcher.** Register a `RateSpec` (in the pack's
   `rates.py`, or `zombiescan/pricing/rates.py` for core) and price the finding
   through `ctx.pricing.rate(...)`. Then register a `@price_fetcher` for the
   table section so `python -m zombiescan.pricing.refresh` can rebuild it —
   `tests/test_packs.py` asserts every section has exactly one fetcher, so a
   missing one fails the suite.

   Find the meter by querying the public Retail Prices API rather than
   guessing, and read it with `unit_price`, not the first row: tier rows come
   back unordered and tier 0 is often a free allowance. Match the meter name,
   not just the SKU — `P80 LRS Disk` and `P80 LRS Disk Mount` share a SKU and
   differ sixteenfold. If `armRegionName` comes back as "Global" or as a
   billing geography like "Zone 1", the meter has no per-region entry and
   belongs in a global section.

8. **Give it a cleaner or a refusal.** Either add a planner to the pack's
   `cleaners.py` (back up before destroying, address the resource by
   `finding.arm_id`, mark `irreversible=True` only where there is no recovery
   window at all — Key Vault soft-delete and SQL point-in-time restore are
   real windows and are not marked) or pass `uncleanable="<why not>"` to
   `@check`. A check with neither fails `test_clean.py`.

9. **Run** `uv run pytest` and `uv run ruff check src tests`, then report the
   new check's output against the fixture. `test_packs.py` also asserts every
   check has a test file of its own, so step 6 is not optional.

Do not run a live scan as part of this skill — that is `/livesmoke`.
