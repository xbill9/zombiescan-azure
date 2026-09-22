"""Disabled Key Vault keys that are still billed."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.disabled_key_vault_key import disabled_key_vault_key

FIXTURE = load_fixture("disabled_key_vault_key")


def _findings(make_context):
    ctx, arm = make_context(
        {"Microsoft.KeyVault/vaults": FIXTURE["vaults"], "/keys": FIXTURE["keys"]}
    )
    return list(disabled_key_vault_key(ctx)), arm


def test_an_enabled_key_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "prod-kv/active-signing" not in {f.resource_id for f in findings}


def test_every_disabled_key_is_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {
        "prod-kv/retired-signing",
        "prod-kv/retired-large",
        "prod-kv/old-software",
    }


def test_an_hsm_key_is_billed_while_disabled(make_context):
    """Disabling stops it being used. It does not stop it being charged."""
    findings, _ = _findings(make_context)
    key = next(f for f in findings if f.resource_id == "prod-kv/retired-signing")
    assert key.monthly_cost == 1.00
    assert "billed per month whether it is enabled or not" in key.reason


def test_a_large_rsa_hsm_key_is_billed_at_the_advanced_rate(make_context):
    findings, _ = _findings(make_context)
    key = next(f for f in findings if f.resource_id == "prod-kv/retired-large")
    assert key.details["billed_as"] == "hsm_advanced"
    assert key.monthly_cost == 5.00


def test_a_software_key_is_free_and_the_finding_says_so(make_context):
    """Key Vault bills software keys per operation, not per key."""
    findings, _ = _findings(make_context)
    key = next(f for f in findings if f.resource_id == "prod-kv/old-software")
    assert key.monthly_cost == 0.0
    assert "carries no monthly charge" in key.reason


def test_soft_delete_is_named_because_it_delays_the_saving(make_context):
    findings, _ = _findings(make_context)
    assert "soft-delete" in findings[0].details["note"]
