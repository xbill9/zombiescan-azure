"""Resource groups holding nothing."""

from __future__ import annotations

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.empty_resource_group import empty_resource_group

FIXTURE = load_fixture("empty_resource_group")

# Resource Manager's own collections are not under /providers/: a resource
# group lists at /subscriptions/<id>/resourcegroups. Keying the fake on the
# provider path instead is exactly the mistake the real code has to avoid,
# because ARM answers that path with 404 rather than with an empty page.
GROUPS_PATH = "/resourcegroups"


def _findings(make_context):
    ctx, arm = make_context(
        {GROUPS_PATH: FIXTURE["groups"]},
        graph={"summarize": FIXTURE["counts"]},
    )
    return list(empty_resource_group(ctx)), arm


def test_groups_are_listed_from_the_control_plane_path(make_context):
    """Not /providers/Microsoft.Resources/resourceGroups, which answers 404."""
    _, arm = _findings(make_context)
    assert [path for _, path in arm.call_log] == [f"/subscriptions/{SUBSCRIPTION}/resourcegroups"]


def test_a_group_with_resources_in_it_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "test-rg" not in {f.resource_id for f in findings}


def test_a_group_azure_manages_is_left_alone(make_context):
    """An AKS node group scaled to zero is empty, and deleting it breaks the cluster."""
    findings, _ = _findings(make_context)
    assert "MC_prod_aks_eastus" not in {f.resource_id for f in findings}


def test_only_the_abandoned_group_is_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"leftover-rg"}


def test_the_count_is_computed_by_resource_graph_not_by_listing(make_context):
    """One query counts every group; listing each group's contents is one call each."""
    _, arm = _findings(make_context)
    assert len(arm.graph_log) == 1
    assert "summarize" in arm.graph_log[0]


def test_the_group_is_its_own_resource_group(make_context):
    findings, _ = _findings(make_context)
    assert findings[0].resource_group == "leftover-rg"


def test_the_command_names_the_group_and_confirms(make_context):
    findings, _ = _findings(make_context)
    assert findings[0].remediation == (
        f"az group delete --name leftover-rg --subscription {SUBSCRIPTION} --yes"
    )
