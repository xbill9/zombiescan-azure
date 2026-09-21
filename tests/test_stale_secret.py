from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.stale_secret import stale_secret

FIXTURE = load_fixture("stale_secret")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "projects.secrets.list": FIXTURE["secrets"],
            "projects.secrets.versions.list": lambda **kw: FIXTURE["versions"][kw["parent"]],
        }
    )
    return {f.resource_id: f for f in stale_secret(ctx)}


def test_a_secret_with_only_old_versions_is_flagged(findings):
    assert "legacy-api-key" in findings


def test_a_recently_rotated_secret_is_left_alone(findings):
    assert "rotated-weekly" not in findings


def test_a_secret_whose_versions_are_all_destroyed_bills_nothing(findings):
    """Nothing is billing, so there is nothing to report."""
    assert "all-versions-destroyed" not in findings


def test_cost_is_per_enabled_version(findings):
    """Google bills per active version, not per secret, so rotation multiplies it."""
    assert findings["legacy-api-key"].details["enabled_versions"] == 1
    assert findings["legacy-api-key"].monthly_cost == pytest.approx(0.06)


def test_a_user_managed_replication_bills_per_replica(findings):
    finding = findings["multi-region-token"]
    assert finding.details["replica_count"] == 3
    assert finding.monthly_cost == pytest.approx(3 * 0.06)


def test_destroyed_versions_are_counted_but_not_billed(findings):
    finding = findings["legacy-api-key"]
    assert finding.details["total_versions"] == 2
    assert finding.details["enabled_versions"] == 1


def test_the_note_is_honest_about_what_age_means(findings):
    """The API does not report access times, so staleness is version age."""
    note = findings["legacy-api-key"].details["note"]
    assert "not time since last access" in note


def test_the_resource_id_is_the_bare_secret_name(findings):
    """The delete command takes the id, not the full resource path."""
    assert findings["legacy-api-key"].remediation == (
        "gcloud secrets delete legacy-api-key --project=test-project --quiet"
    )


def test_secrets_are_global(findings):
    assert all(f.location == "global" for f in findings.values())
