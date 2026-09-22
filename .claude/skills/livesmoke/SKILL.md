---
name: livesmoke
description: Run the opt-in end-to-end zombiescan smoke test against a real Azure subscription, after verifying the Azure CLI is still signed in.
disable-model-invocation: true
---

Run the live end-to-end scan against a real Azure subscription.

This hits real Azure APIs. It is gated behind `ZOMBIESCAN_LIVE=1` and excluded
from the default test run for that reason.

## Steps

1. **Check the credentials before anything else.** `az` tokens expire, and
   expiring mid-scan is the normal failure here:

   ```
   az account get-access-token --resource https://management.azure.com/ \
     --query expiresOn -o tsv
   ```

   If that fails, stop and tell the user to run `az login` — do not start a
   scan that will die partway through. Never print the token itself.

2. **Confirm the subscription** with
   `az account show --query "{name:name, id:id, user:user.name}" -o json` and
   show the user which one is about to be scanned. If the credentials are a
   user account with Owner or Contributor, note that the least-privilege role
   in `policy/` is not being exercised by this run.

3. **Run** `ZOMBIESCAN_LIVE=1 uv run pytest -m live`.

   Two of those tests are not about this subscription's waste. One verifies
   every pinned `api-version` against what ARM currently accepts; the other
   verifies `helpers.CONFIRMS` against the installed Azure CLI. A failure in
   either is a repository bug to fix, not a finding.

4. **Report** the findings count, the monthly waste total, and any check that
   errored. An empty result is a valid outcome, not a failure — say so plainly
   rather than hunting for something to report.

   **Report `pairs_unavailable` next to the total, always.** A check whose
   resource provider is not registered on the subscription was skipped, which
   is a fact about the subscription rather than a problem with the scan — but
   if most of the catalog was skipped, "no waste found" means "no waste found
   in the few services this subscription uses". Say which one happened.

5. If a check raised on a real ARM shape that the fixtures do not cover,
   capture that shape as a new fixture case so the offline suite catches it
   next time. Scrub the subscription and tenant ids first.

Read-only throughout. Never run the generated remediation script.
