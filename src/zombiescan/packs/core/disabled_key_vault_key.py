"""Key Vault keys that are disabled and still billed.

Disabling a key stops it being used. It does not stop it being charged: an
HSM-protected key in a Premium vault bills about $1 a month whether it is
enabled, disabled, or expired years ago. Disabling is the step people take
before deleting, and it is very often the last step anyone takes.

**Software-protected keys are free**, because Key Vault bills software keys
per operation rather than per key. Those are reported at no cost, as hygiene:
a disabled key with an access policy still pointing at it is a permission
grant nobody is reviewing.

Keys are read through ARM's management-plane listing rather than the vault's
data plane. The management plane returns a key's attributes -- enabled, type,
when it was last updated -- without needing a second token audience or a data
plane permission the scanner would have to be granted separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "disabled-key-vault-key"

RESOURCE_TYPE = "Microsoft.KeyVault/vaults"

# Key types Azure stores in hardware and charges a monthly fee for. The
# software equivalents -- RSA, EC -- are free.
HSM_KEY_TYPES = frozenset({"RSA-HSM", "EC-HSM", "oct-HSM"})

# RSA-HSM keys above 2048 bits are billed at the higher "advanced key" rate.
ADVANCED_RSA_BITS = 2048


def _variant(attributes: dict[str, Any], properties: dict[str, Any]) -> str:
    key_type = str(properties.get("kty") or "")
    if key_type not in HSM_KEY_TYPES:
        return "software"
    size = int(properties.get("keySize") or 0)
    if key_type == "RSA-HSM" and size > ADVANCED_RSA_BITS:
        return "hsm_advanced"
    return "hsm"


def build_finding(ctx: ScanContext, vault: dict[str, Any], key: dict[str, Any]) -> Finding:
    name = key["name"]
    arm_id = key.get("id") or ""
    group = vault.get("resourceGroup") or azure.resource_group_of(vault.get("id") or "")
    location = helpers.location_of(vault)
    properties = helpers.properties(key)
    attributes = properties.get("attributes") or {}

    variant = _variant(attributes, properties)
    price, approximate = ctx.pricing.rate("keyvault.key_month", variant=variant)
    updated = helpers.epoch_age_days(attributes.get("updated"))

    if price:
        reason = (
            f"{properties.get('kty')} key in vault {vault['name']} is disabled, and an "
            f"HSM-protected key is billed per month whether it is enabled or not"
        )
    else:
        reason = (
            f"{properties.get('kty')} key in vault {vault['name']} is disabled. A "
            f"software-protected key carries no monthly charge, so this costs nothing"
        )
    if updated is not None:
        reason += f"; last changed {updated} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{vault['name']}/{name}",
        resource_type="key-vault-key",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=price,
        remediation=helpers.az(
            f"az keyvault key delete --name {helpers.arg(name)} "
            f"--vault-name {helpers.arg(vault['name'])}",
            ctx.subscription,
        ),
        approximate_cost=approximate and bool(price),
        details={
            "vault": vault["name"],
            "key": name,
            "key_type": properties.get("kty"),
            "key_size": properties.get("keySize"),
            "billed_as": variant,
            "enabled": attributes.get("enabled"),
            "expires": attributes.get("exp"),
            "last_updated_days_ago": updated,
            "note": (
                "Key Vault soft-delete keeps a deleted key recoverable for the vault's "
                "retention period, which is why deleting one is not marked "
                "irreversible. Purge protection, if the vault has it on, keeps the key "
                "billable until that period expires"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Disabled Key Vault keys still billed",
    providers="Microsoft.KeyVault",
)
def disabled_key_vault_key(ctx: ScanContext) -> Iterator[Finding]:
    # Key Vault offers no subscription-wide key listing: keys are listed per
    # vault. The vaults come back in one call, and the per-vault calls are
    # made in parallel so a subscription with sixty vaults does not spend a
    # minute here.
    vaults = {vault["id"]: vault for vault in ctx.list(RESOURCE_TYPE) if vault.get("id")}

    def keys_of(vault_id: str) -> list[dict[str, Any]]:
        return list(ctx.arm.list(f"{vault_id}/keys", RESOURCE_TYPE))

    for vault_id, key in helpers.across_parents(keys_of, vaults):
        attributes = helpers.properties(key).get("attributes") or {}
        if attributes.get("enabled") is False:
            yield build_finding(ctx, vaults[vault_id], key)
