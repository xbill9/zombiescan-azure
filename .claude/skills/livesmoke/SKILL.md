---
name: livesmoke
description: Run the opt-in end-to-end zombiescan smoke test against a real Google Cloud project, after verifying Application Default Credentials are still valid.
disable-model-invocation: true
---

Run the live end-to-end scan against a real Google Cloud project.

This hits real Google Cloud APIs. It is gated behind `ZOMBIESCAN_LIVE=1` and
excluded from the default test run for that reason.

## Steps

1. **Check the credentials before anything else.** Application Default
   Credentials expire, and expiring mid-scan is the normal failure here:

   ```
   gcloud auth application-default print-access-token --quiet >/dev/null && echo "ADC ok"
   ```

   If that fails, stop and tell the user to run
   `gcloud auth application-default login` — do not start a scan that will die
   partway through. Never print the token itself.

2. **Confirm the project** with
   `gcloud config get-value project --quiet` and show the user which project is
   about to be scanned. If the credentials are a user account with Owner or
   Editor, note that the least-privilege role in `policy/` is not being
   exercised by this run.

3. **Run** `ZOMBIESCAN_LIVE=1 uv run pytest -m live`.

4. **Report** the findings count, the monthly waste total, and any check that
   errored. An empty result is a valid outcome, not a failure — say so plainly
   rather than hunting for something to report.

   Report `pairs_unavailable` separately from errors. A check whose API is not
   enabled on the project was skipped, which is a fact about the project rather
   than a problem with the scan.

5. If a check raised on a real API shape that the fixtures do not cover, capture
   that shape as a new fixture case so the offline suite catches it next time.

Read-only throughout. Never run the generated remediation script.
