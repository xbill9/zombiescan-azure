from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_static_ip import unused_static_ip

FIXTURE = load_fixture("unused_static_ip")


def _run(make_context):
    ctx, client = make_context(
        {
            "addresses.aggregatedList": FIXTURE["regional"],
            "globalAddresses.list": FIXTURE["global"],
        }
    )
    return {f.resource_id: f for f in unused_static_ip(ctx)}, client


@pytest.fixture
def findings(make_context):
    return _run(make_context)[0]


def test_only_reserved_addresses_are_flagged(findings):
    assert set(findings) == {"orphaned-web-ip", "eu-spare-ip", "old-lb-ip"}


def test_an_address_in_use_is_not_waste(findings):
    assert "in-use-ip" not in findings
    assert "live-lb-ip" not in findings


def test_global_addresses_are_checked_too(findings):
    """aggregatedList does not reach them, so a forgotten load balancer IP would hide."""
    assert findings["old-lb-ip"].location == "global"


def test_cost_is_the_idle_hourly_rate_for_a_month(findings):
    # us-central1 idle rate 0.01/hour over a 730-hour month
    assert findings["orphaned-web-ip"].monthly_cost == pytest.approx(7.30)
    assert findings["eu-spare-ip"].monthly_cost == pytest.approx(0.012 * 730)


def test_the_reason_says_which_scope_the_address_is_in(findings):
    assert findings["orphaned-web-ip"].reason.startswith("Regional static IP 34.10.0.1")
    assert findings["old-lb-ip"].reason.startswith("Global static IP 34.99.0.1")


def test_a_global_address_is_deleted_with_the_global_flag(findings):
    """--region on a global address is an error, and the reverse prompts."""
    assert "--global" in findings["old-lb-ip"].remediation
    assert "--region" not in findings["old-lb-ip"].remediation
    assert "--region=us-central1" in findings["orphaned-web-ip"].remediation


def test_every_command_names_the_project_and_is_quiet(findings):
    for finding in findings.values():
        assert "--project=test-project" in finding.remediation
        assert finding.remediation.endswith("--quiet")
