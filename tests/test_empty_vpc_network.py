from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.empty_vpc_network import empty_vpc_network

FIXTURE = load_fixture("empty_vpc_network")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "networks.list": FIXTURE["networks"],
            "instances.aggregatedList": FIXTURE["instances"],
            "routers.aggregatedList": FIXTURE["routers"],
            "subnetworks.aggregatedList": FIXTURE["subnetworks"],
        }
    )
    return {f.resource_id: f for f in empty_vpc_network(ctx)}


def test_networks_with_no_instances_are_flagged(findings):
    assert set(findings) == {"default", "abandoned-vpc"}


def test_a_network_running_something_is_left_alone(findings):
    assert "prod" not in findings


def test_the_auto_created_default_network_is_called_out(findings):
    """Nobody chose it, and it is the most common empty VPC by a wide margin."""
    finding = findings["default"]
    assert finding.details["is_default_network"] is True
    assert "auto-created default network" in finding.reason


def test_the_cost_is_the_priced_waste_inside_it(findings):
    """A VPC is free; the NAT addresses inside it are not."""
    # three reserved NAT addresses at 0.005/hour over 730 hours
    assert findings["abandoned-vpc"].monthly_cost == pytest.approx(3 * 0.005 * 730)
    assert findings["default"].monthly_cost == 0.0


def test_the_inventory_says_what_is_still_in_there(findings):
    contents = findings["abandoned-vpc"].details["contents"]
    assert contents == {"subnets": 1, "cloud routers": 1, "reserved NAT addresses": 3}


def test_the_reason_lists_what_the_network_holds(findings):
    assert "1 subnets" in findings["abandoned-vpc"].reason
    assert "3 reserved NAT addresses" in findings["abandoned-vpc"].reason


def test_an_empty_network_holding_nothing_says_so(findings):
    assert "holds none" in findings["default"].reason


def test_a_free_finding_is_never_marked_approximate(findings):
    """Zero here is exact: the network holds nothing priced."""
    assert findings["default"].approximate_cost is False
