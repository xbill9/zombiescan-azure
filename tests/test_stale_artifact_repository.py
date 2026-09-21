from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.stale_artifact_repository import stale_artifact_repository

FIXTURE = load_fixture("stale_artifact_repository")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "projects.locations.list": FIXTURE["locations"],
            "projects.locations.repositories.list": (
                lambda **kw: FIXTURE["repositories"][kw["parent"]]
            ),
        }
    )
    return {f.resource_id: f for f in stale_artifact_repository(ctx)}


def test_a_repository_with_no_recent_push_is_flagged(findings):
    assert "abandoned-ci" in findings


def test_a_repository_pushed_to_recently_is_left_alone(findings):
    assert "active-ci" not in findings


def test_an_empty_repository_is_not_reported(findings):
    """It is old, but it costs nothing, so reporting it is noise."""
    assert "empty-old" not in findings


def test_every_location_is_walked(findings):
    """Artifact Registry rejects locations/-, so the check enumerates them."""
    assert "eu-stale" in findings
    assert findings["eu-stale"].location == "europe-west4"


def test_cost_is_the_stored_size_at_one_global_rate(findings):
    # 64 GiB at the flat Artifact Registry rate of 0.10
    assert findings["abandoned-ci"].monthly_cost == pytest.approx(64 * 0.10)


def test_the_figure_is_always_marked_as_an_upper_bound(findings):
    """Artifact Registry bills each unique layer once; sizeBytes counts shared
    base layers once per image."""
    finding = findings["abandoned-ci"]
    assert finding.approximate_cost is True
    assert "upper bound" in finding.details["note"]
    assert finding.details["rate_is_exact"] is True


def test_the_reason_names_the_format_and_the_idle_time(findings):
    reason = findings["abandoned-ci"].reason
    assert reason.startswith("DOCKER repository holding 64.0 GB")
    assert "200 days" in reason


def test_deletion_names_the_location(findings):
    assert findings["eu-stale"].remediation == (
        "gcloud artifacts repositories delete eu-stale --location=europe-west4 "
        "--project=test-project --quiet"
    )
