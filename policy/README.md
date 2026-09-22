# Least privilege

`zombiescan-scanner-role.json` is an Azure custom role holding exactly the
permissions a scan uses: one read per resource type the checks call, and
nothing that can change anything.

```
az role definition create --role-definition policy/zombiescan-scanner-role.json

az role assignment create \
  --assignee you@example.com \
  --role "zombiescan Scanner" \
  --scope /subscriptions/SUBSCRIPTION_ID
```

Edit `assignableScopes` first: it must name the subscription, management group
or resource group the role may be assigned at. To scan many subscriptions,
create the role against a management group
(`/providers/Microsoft.Management/managementGroups/GROUP_ID`) and assign it
there; the assignment is inherited and `--all-subscriptions` then sees
everything it covers.

## Keeping it in step

Every check declares the resource providers it reads. `zombiescan providers`
prints them, and `tests/test_packs.py` fails if a check names a provider this
role does not cover. A new check that reads a new provider needs a line here,
or a scan running under this role reports nothing for it.

**On Azure that failure is quiet in two ways at once.** A resource type the
role does not cover is filtered out of the list rather than refused, so the
call succeeds and returns fewer resources; and a provider that is not
registered on the subscription returns an empty page with HTTP 200. Neither
looks like an error. The engine handles the second by reading each
subscription's provider registrations before it runs a check, and the suite
handles the first by generating this list from the same declarations.

`Microsoft.ResourceGraph/resources/read` is what lets Resource Graph answer at
all. Resource Graph returns only resources the caller can already read, so it
grants no reach beyond the rest of this list — it is the query surface, not an
extra permission.

## Cleaning needs more, on purpose

`zombiescan clean --apply` deletes things, and none of the permissions here let
it. That separation is deliberate: the identity that scans should not be able
to remove what it finds. Grant the delete permissions to a principal used only
for cleanup, and only when you are running one.

## Not yet verified against a scoped principal

This role is assembled from the calls the checks make, not from a run that was
constrained by it. Credentials with Owner or Contributor are not limited by
Azure RBAC in the way this role describes, so a scan under one of those does
not exercise it. Assign the role to a dedicated service principal and scan
with that before relying on the claim.
