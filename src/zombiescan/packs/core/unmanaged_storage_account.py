"""Storage accounts that keep every version and prune none of them.

Blob versioning keeps every overwrite and every delete as a previous version,
and a previous version bills at the full storage rate of its access tier.
Versioning with no lifecycle management policy to expire those versions is
therefore an account that grows forever, and it grows fastest where it is
least visible: a build artifact overwritten nightly keeps a year of copies.

Blob soft delete has the same shape -- deleted blobs are retained and billed
for their retention period -- but it expires on its own, so it is recorded in
the finding rather than being the reason for it.

Turning versioning on without a lifecycle policy is a configuration mistake
rather than a leftover resource, which is why the remediation adds a policy
instead of deleting anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.azure import ArmError
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unmanaged-storage-account"

RESOURCE_TYPE = "Microsoft.Storage/storageAccounts"

SUGGESTED_POLICY = {
    "rules": [
        {
            "name": "expire-old-versions",
            "enabled": True,
            "type": "Lifecycle",
            "definition": {
                "filters": {"blobTypes": ["blockBlob"]},
                "actions": {
                    "version": {"delete": {"daysAfterCreationGreaterThan": 30}},
                },
            },
        }
    ]
}


def _prunes_versions(policy: dict[str, Any]) -> bool:
    """Whether a lifecycle policy actually deletes previous versions.

    A rule that only moves blobs to a cooler tier slows the growth; it does
    not stop it. Only a ``version.delete`` or ``baseBlob.delete`` action
    removes anything.
    """
    for rule in policy.get("rules") or []:
        if not rule.get("enabled", True):
            continue
        actions = ((rule.get("definition") or {}).get("actions")) or {}
        if (actions.get("version") or {}).get("delete"):
            return True
        if (actions.get("baseBlob") or {}).get("delete"):
            return True
    return False


def build_finding(ctx: ScanContext, account: dict[str, Any], blob: dict[str, Any]) -> Finding:
    name = account["name"]
    arm_id = account.get("id") or ""
    group = account.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(account)
    properties = helpers.properties(account)
    blob_properties = helpers.properties(blob)

    price, approximate = ctx.pricing.rate("blob.gb_month", region=azure.region_of(location))
    soft_delete = blob_properties.get("deleteRetentionPolicy") or {}

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="storage-account",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            "Storage account has blob versioning on and no lifecycle policy that "
            "expires previous versions, so every overwrite is kept and billed forever"
        ),
        # The account listing does not report how many bytes it holds, and
        # asking would mean enumerating every blob in every container.
        # Reporting unbounded growth is honest; inventing a size would not be.
        monthly_cost=0.0,
        remediation=helpers.az(
            f"az storage account management-policy create --account-name {helpers.arg(name)} "
            "--policy @lifecycle.json",
            ctx.subscription,
            group,
        ),
        approximate_cost=approximate,
        details={
            "sku": (account.get("sku") or {}).get("name"),
            "access_tier": properties.get("accessTier"),
            "versioning_enabled": True,
            "change_feed_enabled": bool((blob_properties.get("changeFeed") or {}).get("enabled")),
            "blob_soft_delete_days": (
                soft_delete.get("days") if soft_delete.get("enabled") else None
            ),
            "tags": account.get("tags") or {},
            "usd_per_gb_month": price,
            "note": (
                "unpriced: the account listing does not report stored bytes, and "
                "counting them would mean enumerating every blob. Reported as "
                "unbounded growth"
            ),
            "suggested_lifecycle_policy": SUGGESTED_POLICY,
        },
    )


@check(
    CHECK_NAME,
    "Versioned storage accounts with no lifecycle policy",
    providers="Microsoft.Storage",
    uncleanable=(
        "the right lifecycle policy depends on how many versions the account must "
        "keep and for how long, which nothing in the API says. zombiescan reports a "
        "suggested policy in the finding's details and leaves applying it to you"
    ),
)
def unmanaged_storage_account(ctx: ScanContext) -> Iterator[Finding]:
    for account in ctx.list(RESOURCE_TYPE):
        account_id = account.get("id") or ""
        if not account_id:
            continue
        try:
            blob = ctx.arm.get(f"{account_id}/blobServices/default", RESOURCE_TYPE)
        except ArmError:
            # An account with no blob service, or one behind a firewall that
            # refuses the read. Neither is evidence of unbounded growth.
            continue
        blob_properties = helpers.properties(blob)
        if not (blob_properties.get("isVersioningEnabled")):
            continue
        try:
            policy = (
                helpers.properties(
                    ctx.arm.get(f"{account_id}/managementPolicies/default", RESOURCE_TYPE)
                ).get("policy")
                or {}
            )
        except ArmError:
            # ARM answers 404 when no policy exists, which is exactly the
            # case this check is looking for.
            policy = {}
        if _prunes_versions(policy):
            continue
        yield build_finding(ctx, account, blob)
