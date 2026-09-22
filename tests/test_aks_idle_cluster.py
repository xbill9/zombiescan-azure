"""AKS clusters running no nodes, and the tier that decides what they cost."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.aks.idle_cluster import aks_idle_cluster


def _findings(make_context):
    ctx, arm = make_context(
        {"Microsoft.ContainerService/managedClusters": load_fixture("aks_idle_cluster")}
    )
    return list(aks_idle_cluster(ctx)), arm


def test_a_cluster_with_nodes_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "prod-aks" not in {f.resource_id for f in findings}


def test_a_standard_tier_cluster_pays_its_fee_with_no_nodes(make_context):
    """$0.10/hour, about $73 a month, for a cluster running nothing."""
    findings, _ = _findings(make_context)
    cluster = next(f for f in findings if f.resource_id == "scaled-to-zero")
    assert cluster.monthly_cost == 0.10 * 730
    assert "charged per hour regardless" in cluster.reason


def test_a_free_tier_cluster_really_is_free(make_context):
    """The reverse of GKE, where the management fee is charged whatever the tier."""
    findings, _ = _findings(make_context)
    cluster = next(f for f in findings if f.resource_id == "free-sandbox")
    assert cluster.monthly_cost == 0.0
    assert "is not charged, so this costs nothing today" in cluster.reason
    assert cluster.approximate_cost is False


def test_the_node_pools_are_reported_so_the_zero_is_checkable(make_context):
    findings, _ = _findings(make_context)
    cluster = next(f for f in findings if f.resource_id == "scaled-to-zero")
    assert cluster.details["node_pools"] == [
        {"name": "system", "nodes": 0, "vm_size": "Standard_D2s_v5", "mode": "System"}
    ]


def test_the_cost_is_the_control_plane_only(make_context):
    findings, _ = _findings(make_context)
    cluster = next(f for f in findings if f.resource_id == "scaled-to-zero")
    assert "control plane only" in cluster.details["note"]
    assert "unattached-disk" in cluster.details["note"]
