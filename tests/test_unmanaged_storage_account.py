"""Storage accounts that keep every version and prune none of them."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.azure import ArmError
from zombiescan.packs.core.unmanaged_storage_account import (
    CHECK_NAME,
    unmanaged_storage_account,
)
from zombiescan.registry import CHECKS

FIXTURE = load_fixture("unmanaged_storage_account")

_NO_POLICY = ArmError(404, "ResourceNotFound", "no management policy")


def _blob_for(path: str):
    return FIXTURE["blob_plain"] if "plainstore" in path else FIXTURE["blob_versioned"]


def _policy_for(path: str):
    if "managedlogs" in path:
        return FIXTURE["policy_prunes"]
    if "buildartifacts" in path:
        return FIXTURE["policy_tiering_only"]
    return _NO_POLICY


def _findings(make_context, policy=_policy_for):
    ctx, arm = make_context(
        {
            "Microsoft.Storage/storageAccounts": FIXTURE["accounts"],
            "/blobServices/default": lambda path: _blob_for(path),
            "/managementPolicies/default": lambda path: policy(path),
        }
    )
    return list(unmanaged_storage_account(ctx)), arm


def test_an_account_without_versioning_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "plainstore" not in {f.resource_id for f in findings}


def test_a_policy_that_deletes_versions_is_enough(make_context):
    findings, _ = _findings(make_context)
    assert "managedlogs" not in {f.resource_id for f in findings}


def test_a_policy_that_only_changes_tier_does_not_stop_the_growth(make_context):
    """Moving blobs to cool storage slows the bill; it does not bound it."""
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"buildartifacts"}


def test_no_policy_at_all_is_the_case_the_check_is_looking_for(make_context):
    """ARM answers 404 when there is no policy, which must read as 'none'."""
    findings, _ = _findings(make_context, policy=lambda path: _NO_POLICY)
    assert {f.resource_id for f in findings} == {"buildartifacts", "managedlogs"}


def test_it_is_reported_as_growth_rather_than_priced(make_context):
    findings, _ = _findings(make_context)
    assert findings[0].monthly_cost == 0.0
    assert "unpriced" in findings[0].details["note"]


def test_soft_delete_is_recorded_rather_than_being_the_reason(make_context):
    """Soft-deleted blobs expire on their own; versions do not."""
    findings, _ = _findings(make_context)
    assert findings[0].details["blob_soft_delete_days"] == 7


def test_the_check_suggests_a_policy_instead_of_deleting_anything(make_context):
    spec = CHECKS[CHECK_NAME]
    assert spec.uncleanable
    findings, _ = _findings(make_context)
    rule = findings[0].details["suggested_lifecycle_policy"]["rules"][0]
    assert rule["definition"]["actions"]["version"]["delete"]
