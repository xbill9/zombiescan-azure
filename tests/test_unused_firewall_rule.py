from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_firewall_rule import unused_firewall_rule

FIXTURE = load_fixture("unused_firewall_rule")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "firewalls.list": FIXTURE["firewalls"],
            "instances.aggregatedList": FIXTURE["instances"],
        }
    )
    return {f.resource_id: f for f in unused_firewall_rule(ctx)}


def test_a_disabled_rule_is_flagged(findings):
    assert "allow-ssh-disabled" in findings
    assert "is disabled" in findings["allow-ssh-disabled"].reason


def test_a_rule_targeting_a_tag_nothing_carries_is_flagged(findings):
    assert "allow-old-worker-port" in findings
    assert "retired-worker" in findings["allow-old-worker-port"].reason


def test_a_rule_targeting_a_live_tag_is_left_alone(findings):
    assert "allow-web" not in findings


def test_a_rule_with_no_target_tags_is_broad_not_unused(findings):
    """No target tags means it applies to every instance in the network."""
    assert "allow-everything-in-vpc" not in findings


def test_a_tag_is_only_live_inside_its_own_network(findings):
    """The same tag name in another VPC does not make this rule match anything."""
    assert "tag-used-in-another-vpc" in findings


def test_firewall_rules_are_reported_as_hygiene_not_cost(findings):
    """They are free. The finding exists so the rule can be accounted for."""
    assert all(f.monthly_cost == 0.0 for f in findings.values())


def test_the_network_is_surfaced(findings):
    assert findings["allow-old-worker-port"].details["network"] == "prod"


def test_firewall_rules_are_global_resources(findings):
    assert all(f.location == "global" for f in findings.values())


def test_the_delete_command_takes_no_location_flag(findings):
    assert findings["allow-ssh-disabled"].remediation == (
        "gcloud compute firewall-rules delete allow-ssh-disabled --project=test-project --quiet"
    )
