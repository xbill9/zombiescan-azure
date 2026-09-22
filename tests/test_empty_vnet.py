"""Virtual networks with nothing running in them."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.empty_vnet import CHECK_NAME, RESOURCE_TYPE, empty_vnet
from zombiescan.registry import CHECKS

FIXTURE = load_fixture("empty_vnet")


def _findings(make_context):
    ctx, arm = make_context(
        {RESOURCE_TYPE: FIXTURE["vnets"], "Microsoft.Network/natGateways": FIXTURE["nat"]}
    )
    return list(empty_vnet(ctx)), arm


def test_a_network_with_a_nic_in_it_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"abandoned-vnet"}


def test_the_cost_is_the_priced_waste_inside_it(make_context):
    """A VNet is free. The NAT gateway sitting in it is not."""
    findings, _ = _findings(make_context)
    network = findings[0]
    assert network.monthly_cost == 0.045 * 730
    assert network.details["contents"]["NAT gateways"] == 1
    assert "not the network itself" in network.details["note"]


def test_the_check_refuses_to_clean_and_names_the_ordering_problem(make_context):
    spec = CHECKS[CHECK_NAME]
    assert spec.uncleanable
    assert "everything inside it is gone" in spec.uncleanable
