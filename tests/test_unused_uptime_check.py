from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_uptime_check import unused_uptime_check

FIXTURE = load_fixture("unused_uptime_check")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        by_api={
            "monitoring": {"projects.uptimeCheckConfigs.list": FIXTURE["configs"]},
            "compute": {"instances.aggregatedList": FIXTURE["instances"]},
        }
    )
    return {f.resource_id: f for f in unused_uptime_check(ctx)}


def test_a_check_watching_a_deleted_instance_is_flagged(findings):
    assert "deleted-vm-check" in findings


def test_a_check_watching_a_live_instance_is_left_alone(findings):
    assert "live-vm-check" not in findings


def test_an_external_url_check_is_never_judged(findings):
    """Nothing in the project says whether that hostname is still meant to be up."""
    assert "external-url-check" not in findings


def test_it_is_reported_for_the_alerts_not_the_bill(findings):
    finding = findings["deleted-vm-check"]
    assert finding.monthly_cost == 0.0
    assert "free monthly allowance" in finding.details["note"]


def test_the_reason_names_the_missing_instance(findings):
    assert "9999999999" in findings["deleted-vm-check"].reason


def test_the_display_name_is_surfaced(findings):
    assert findings["deleted-vm-check"].details["display_name"] == "API health (old box)"


def test_the_delete_command_takes_the_bare_check_id(findings):
    assert findings["deleted-vm-check"].remediation == (
        "gcloud monitoring uptime delete deleted-vm-check --project=test-project --quiet"
    )
