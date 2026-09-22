"""Secrets nobody has rotated."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.stale_key_vault_secret import (
    CHECK_NAME,
    STALE_AFTER_DAYS,
    stale_key_vault_secret,
)
from zombiescan.registry import CHECKS

FIXTURE = load_fixture("stale_key_vault_secret")


def _findings(make_context):
    ctx, arm = make_context(
        {"Microsoft.KeyVault/vaults": FIXTURE["vaults"], "/secrets": FIXTURE["secrets"]}
    )
    return list(stale_key_vault_secret(ctx)), arm


def test_a_recently_rotated_secret_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"prod-kv/legacy-api-token"}


def test_the_age_comes_from_the_updated_attribute(make_context):
    """`updated` moves when a new version is added, which is the rotation event."""
    findings, _ = _findings(make_context)
    assert findings[0].details["age_days"] >= STALE_AFTER_DAYS


def test_a_forgotten_secret_costs_nothing_on_azure(make_context):
    """Key Vault bills per operation. Secret Manager on Google bills per version."""
    findings, _ = _findings(make_context)
    assert findings[0].monthly_cost == 0.0
    assert "per operation rather than per secret" in findings[0].details["note"]


def test_the_check_refuses_to_clean_and_says_why(make_context):
    spec = CHECKS[CHECK_NAME]
    assert spec.uncleanable
    assert "takes an application down" in spec.uncleanable


def test_the_finding_never_carries_a_secret_value(make_context):
    """The management plane returns attributes only, and nothing here asks for more."""
    findings, _ = _findings(make_context)
    assert "value" not in findings[0].details
    assert "value" not in findings[0].reason
