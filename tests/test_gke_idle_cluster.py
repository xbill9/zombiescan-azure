from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.gke.idle_cluster import gke_idle_cluster


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context({"projects.locations.clusters.list": load_fixture("gke_idle_cluster")})
    return {f.resource_id: f for f in gke_idle_cluster(ctx)}


def test_a_cluster_scaled_to_zero_nodes_is_flagged(findings):
    """Scaling every pool to zero removes the node cost and leaves the
    management fee exactly where it was."""
    assert "scaled-to-zero" in findings


def test_an_autopilot_cluster_with_no_workloads_is_flagged(findings):
    assert "empty-autopilot" in findings
    assert findings["empty-autopilot"].details["autopilot"] is True


def test_a_cluster_running_nodes_is_left_alone(findings):
    assert "prod" not in findings


def test_the_fee_is_flat_and_the_same_everywhere(findings):
    # 0.10/hour over a 730-hour month, regardless of region or cluster size
    assert findings["scaled-to-zero"].monthly_cost == pytest.approx(73.0)
    assert findings["empty-autopilot"].monthly_cost == pytest.approx(73.0)


def test_the_node_pools_are_reported_so_the_zero_is_visible(findings):
    pools = findings["scaled-to-zero"].details["node_pools"]
    assert [p["nodes"] for p in pools] == [0, 0]
    assert [p["machine_type"] for p in pools] == ["e2-medium", "n2-standard-8"]


def test_the_free_tier_caveat_is_stated(findings):
    """One zonal cluster per billing account is free, and a project scan
    cannot see which one that is."""
    assert "per billing account" in findings["scaled-to-zero"].details["note"]


def test_both_zonal_and_regional_clusters_are_covered(findings):
    assert findings["scaled-to-zero"].location == "us-central1-a"
    assert findings["empty-autopilot"].location == "europe-west1"


def test_deletion_names_the_location(findings):
    assert findings["empty-autopilot"].remediation == (
        "gcloud container clusters delete empty-autopilot --location=europe-west1 "
        "--project=test-project --quiet"
    )


def test_one_wildcard_call_covers_zonal_and_regional_clusters(make_context):
    ctx, client = make_context(
        {"projects.locations.clusters.list": load_fixture("gke_idle_cluster")}
    )
    list(gke_idle_cluster(ctx))
    assert client.calls["projects.locations.clusters.list"] == {
        "parent": "projects/test-project/locations/-"
    }


def test_the_cleaner_marks_deletion_irreversible(pricing):
    from zombiescan import clean
    from zombiescan.models import Finding

    finding = Finding(
        check="gke-idle-cluster",
        resource_id="empty-autopilot",
        resource_type="gke-cluster",
        project="test-project",
        location="europe-west1",
        reason="no nodes",
        monthly_cost=73.0,
        remediation="gcloud container clusters delete empty-autopilot",
    )
    outcome = clean.plan_for(None, finding, pricing)
    assert outcome.status == clean.PLANNED
    assert outcome.irreversible is True
    assert outcome.steps[0].params["name"] == (
        "projects/test-project/locations/europe-west1/clusters/empty-autopilot"
    )
