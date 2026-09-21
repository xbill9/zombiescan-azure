from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unmanaged_gcs_bucket import unmanaged_gcs_bucket


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context({"buckets.list": load_fixture("unmanaged_gcs_bucket")})
    return {f.resource_id: f for f in unmanaged_gcs_bucket(ctx)}


def test_a_versioned_bucket_with_no_pruning_rule_is_flagged(findings):
    assert "build-artifacts-unpruned" in findings


def test_a_bucket_with_a_version_expiry_rule_is_left_alone(findings):
    assert "versioned-and-pruned" not in findings


def test_a_rule_that_only_changes_storage_class_does_not_count(findings):
    """It slows the growth; it does not stop it."""
    assert "versioned-class-change-only" in findings


def test_a_bucket_without_versioning_is_not_a_finding(findings):
    assert "not-versioned" not in findings


def test_the_location_is_lowercased_to_match_the_price_table(findings):
    assert findings["build-artifacts-unpruned"].location == "us-central1"
    assert findings["build-artifacts-unpruned"].approximate_cost is False


def test_it_is_reported_as_growth_rather_than_a_dollar_figure(findings):
    """Counting the bytes would mean listing every object in the bucket."""
    finding = findings["build-artifacts-unpruned"]
    assert finding.monthly_cost == 0.0
    assert "unpriced" in finding.details["note"]
    assert finding.details["usd_per_gb_month"] == 0.02


def test_a_concrete_lifecycle_rule_is_suggested(findings):
    rule = findings["build-artifacts-unpruned"].details["suggested_lifecycle_rule"]
    assert rule["action"] == {"type": "Delete"}
    assert rule["condition"]["numNewerVersions"] == 3


def test_the_check_refuses_to_clean_and_says_why():
    from zombiescan.registry import CHECKS

    reason = CHECKS["unmanaged-gcs-bucket"].uncleanable
    assert reason and "how many versions the bucket must keep" in reason
