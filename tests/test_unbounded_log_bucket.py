from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unbounded_log_bucket import unbounded_log_bucket


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context({"projects.locations.buckets.list": load_fixture("unbounded_log_bucket")})
    return {f.resource_id: f for f in unbounded_log_bucket(ctx)}


def test_a_bucket_retaining_far_past_the_free_window_is_flagged(findings):
    assert "_Default" in findings


def test_a_retention_of_zero_means_never_expire(findings):
    finding = findings["audit-forever"]
    assert finding.details["never_expires"] is True
    assert "never expires its contents" in finding.reason


def test_a_bucket_inside_the_free_window_is_left_alone(findings):
    assert "short-lived" not in findings


def test_a_year_of_retention_is_not_yet_flagged(findings):
    """The threshold is deliberate: a year is a policy, ten is an oversight."""
    assert "one-year" not in findings


def test_the_required_bucket_is_never_reported(findings):
    """Google fixes it at 400 days and nothing can change it, so it is noise."""
    assert "_Required" not in findings


def test_the_location_comes_out_of_the_resource_path(findings):
    assert findings["audit-forever"].location == "us-central1"
    assert findings["_Default"].location == "global"


def test_it_is_reported_as_growth_rather_than_a_dollar_figure(findings):
    """The API does not say how many bytes a bucket holds, and inventing a
    volume would be worse than reporting none."""
    finding = findings["_Default"]
    assert finding.monthly_cost == 0.0
    assert "unpriced" in finding.details["note"]
    assert finding.details["usd_per_gb_month_beyond_free"] == 0.01


def test_the_remediation_shortens_retention_rather_than_deleting(findings):
    assert findings["audit-forever"].remediation == (
        "gcloud logging buckets update audit-forever --location=us-central1 "
        "--retention-days=30 --project=test-project --quiet"
    )


def test_the_check_refuses_to_clean_because_it_would_destroy_logs():
    from zombiescan.registry import CHECKS

    reason = CHECKS["unbounded-log-bucket"].uncleanable
    assert reason and "compliance" in reason
