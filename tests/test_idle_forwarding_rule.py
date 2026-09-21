from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.idle_forwarding_rule import idle_forwarding_rule

FIXTURE = load_fixture("idle_forwarding_rule")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "backendServices.aggregatedList": FIXTURE["backend_services"],
            "forwardingRules.aggregatedList": FIXTURE["regional"],
            "globalForwardingRules.list": FIXTURE["global_rules"],
        }
    )
    return {f.resource_id: f for f in idle_forwarding_rule(ctx)}


def test_a_rule_whose_backend_service_is_empty_is_flagged(findings):
    assert "internal-lb-no-backends" in findings
    assert "public-lb-no-backends" in findings


def test_a_rule_with_a_live_backend_is_left_alone(findings):
    assert "public-lb-live" not in findings


def test_a_rule_pointing_at_nothing_at_all_is_flagged(findings):
    finding = findings["rule-pointing-nowhere"]
    assert "points at no target" in finding.reason


def test_global_rules_are_checked_too(findings):
    """aggregatedList does not reach them, and they front the expensive load balancers."""
    assert findings["public-lb-no-backends"].location == "global"


def test_cost_is_the_forwarding_rule_minimum(findings):
    # us-central1 at 0.025/hour over 730 hours
    assert findings["internal-lb-no-backends"].monthly_cost == pytest.approx(0.025 * 730)


def test_the_note_says_data_processing_is_excluded(findings):
    assert "data processing" in findings["internal-lb-no-backends"].details["note"]


def test_the_reason_names_the_empty_backend_service(findings):
    assert "regional-empty-backend" in findings["internal-lb-no-backends"].reason


def test_a_global_rule_is_deleted_with_the_global_flag(findings):
    assert "--global" in findings["public-lb-no-backends"].remediation
    assert "--region=us-central1" in findings["internal-lb-no-backends"].remediation
