"""Key Vault secrets nobody has rotated.

A secret with no new version in ninety days is either a credential that should
have been rotated and was not, or one for a system that no longer exists.
Both are worth knowing about, and neither shows up anywhere else.

**This one costs nothing, and that is the finding.** Key Vault bills per
operation, not per secret, so a thousand forgotten secrets are free to store.
The Google Cloud equivalent is not: Secret Manager charges $0.06 per active
version per replica per month, so the same pile of forgotten secrets there is
a line item. On Azure the reason to clean them up is that an unrotated
credential is a credential, not that it is expensive.

Secrets are read through ARM's management-plane listing, which returns a
secret's attributes without returning its value and without needing data-plane
access. **zombiescan never reads a secret's value**, and the read-only role in
``policy/`` grants no permission that would let it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "stale-key-vault-secret"

RESOURCE_TYPE = "Microsoft.KeyVault/vaults"

STALE_AFTER_DAYS = 90


def build_finding(ctx: ScanContext, vault: dict[str, Any], secret: dict[str, Any], age: int):
    name = secret["name"]
    arm_id = secret.get("id") or ""
    group = vault.get("resourceGroup") or azure.resource_group_of(vault.get("id") or "")
    location = helpers.location_of(vault)
    properties = helpers.properties(secret)
    attributes = properties.get("attributes") or {}

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{vault['name']}/{name}",
        resource_type="key-vault-secret",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"Secret in vault {vault['name']} has had no new version in {age} days, "
            f"past the {STALE_AFTER_DAYS}-day rotation window"
        ),
        # Key Vault charges per operation, not per stored secret.
        monthly_cost=0.0,
        remediation=helpers.az(
            f"az keyvault secret delete --name {helpers.arg(name)} "
            f"--vault-name {helpers.arg(vault['name'])}",
            ctx.subscription,
        ),
        details={
            "vault": vault["name"],
            "secret": name,
            "age_days": age,
            "enabled": attributes.get("enabled"),
            "expires": attributes.get("exp"),
            "content_type": properties.get("contentType"),
            "note": (
                "no charge: Key Vault bills per operation rather than per secret. "
                "Reported because an unrotated credential is a credential"
            ),
        },
    )


@check(
    CHECK_NAME,
    f"Secrets with no new version in {STALE_AFTER_DAYS} days",
    providers="Microsoft.KeyVault",
    uncleanable=(
        "a secret with no recent version is as likely to be a credential in daily "
        "use that nobody rotates as one for a system that is gone, and nothing in "
        "the management plane says which -- the read count that would is in the "
        "vault's diagnostic logs. Deleting a live credential takes an application "
        "down, so zombiescan reports it and leaves the decision to you"
    ),
)
def stale_key_vault_secret(ctx: ScanContext) -> Iterator[Finding]:
    # Secrets are listed per vault, like keys, and walked in parallel for the
    # same reason.
    vaults = {vault["id"]: vault for vault in ctx.list(RESOURCE_TYPE) if vault.get("id")}

    def secrets_of(vault_id: str) -> list[dict[str, Any]]:
        return list(ctx.arm.list(f"{vault_id}/secrets", RESOURCE_TYPE))

    for vault_id, secret in helpers.across_parents(secrets_of, vaults):
        attributes = helpers.properties(secret).get("attributes") or {}
        # `updated` moves when a new version is added, which is exactly the
        # rotation event being looked for.
        age = helpers.epoch_age_days(attributes.get("updated"))
        if age is not None and age >= STALE_AFTER_DAYS:
            yield build_finding(ctx, vaults[vault_id], secret, age)
