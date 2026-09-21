# Least privilege

`zombiescan-scanner-role.yaml` is a Google Cloud custom role holding exactly
the permissions a scan uses: one list or get per API the checks call, and
nothing that can change anything.

```
gcloud iam roles create zombiescanScanner --project=PROJECT_ID \
  --file=policy/zombiescan-scanner-role.yaml --quiet

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=user:you@example.com \
  --role=projects/PROJECT_ID/roles/zombiescanScanner --quiet
```

To scan many projects, create the role on the organization instead
(`--organization=ORG_ID`) and grant it at the folder or organization level; the
binding is inherited and `--all-projects` then sees everything it covers.

## Keeping it in step

Every check declares the APIs it calls. `zombiescan apis` prints them, and each
permission above is annotated with the check that needs it. A new check that
calls a new API needs a line here, or a scan running under this role reports
nothing for it and looks like a clean project.

## Cleaning needs more, on purpose

`zombiescan clean --apply` deletes things, and none of the permissions here let
it. That separation is deliberate: the identity that scans should not be able
to remove what it finds. Grant the delete permissions to a principal used only
for cleanup, and only when you are running one.

## Not yet verified against a scoped principal

This role is assembled from the calls the checks make, not from a run that was
constrained by it. Credentials with Owner or Editor are not limited by IAM in
the way this role describes, so a scan under one of those does not exercise it.
Bind the role to a dedicated service account and scan with that before relying
on the claim.
