from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_subnet import unused_subnet

FIXTURE = load_fixture("unused_subnet")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "subnetworks.aggregatedList": FIXTURE["subnetworks"],
            "instances.aggregatedList": FIXTURE["instances"],
        }
    )
    return {f.resource_id: f for f in unused_subnet(ctx)}


def test_a_subnet_in_a_network_with_no_instances_is_flagged(findings):
    assert "abandoned-subnet" in findings


def test_a_subnet_in_a_live_network_is_left_alone(findings):
    """A spare subnet in a working VPC may be waiting for a workload."""
    assert "prod-subnet" not in findings


def test_a_subnet_google_uses_for_its_own_plumbing_is_never_reported(findings):
    """Deleting a proxy-only subnet breaks the load balancer it serves."""
    assert "proxy-only-subnet" not in findings


def test_subnets_are_reported_as_hygiene_not_cost(findings):
    assert findings["abandoned-subnet"].monthly_cost == 0.0
    assert "cannot be reused" in findings["abandoned-subnet"].details["note"]


def test_the_reason_names_every_range_the_subnet_holds(findings):
    reason = findings["abandoned-subnet"].reason
    assert "10.128.0.0/20" in reason
    assert "10.200.0.0/14" in reason


def test_secondary_ranges_are_surfaced_with_their_names(findings):
    assert findings["abandoned-subnet"].details["secondary_ranges"] == [
        {"name": "pods", "range": "10.200.0.0/14"}
    ]


def test_deletion_names_the_region(findings):
    assert findings["abandoned-subnet"].remediation == (
        "gcloud compute networks subnets delete abandoned-subnet "
        "--region=us-central1 --project=test-project --quiet"
    )
