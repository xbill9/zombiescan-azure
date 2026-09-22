"""Availability tests watching something that is gone."""

from __future__ import annotations

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.unused_availability_test import (
    RESOURCE_TYPE,
    unused_availability_test,
)

FIXTURE = load_fixture("unused_availability_test")


def _findings(make_context):
    ctx, arm = make_context(
        {RESOURCE_TYPE: FIXTURE["webtests"]},
        graph={"microsoft.insights/components": FIXTURE["components"]},
    )
    return list(unused_availability_test(ctx)), arm


def test_a_test_whose_component_still_exists_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "home-ping" not in {f.resource_id for f in findings}


def test_the_link_is_read_from_the_tag_key_not_the_value(make_context):
    """Azure records it as a tag *named* hidden-link:<id>, valued "Resource".

    Reading the values instead finds nothing and reports every test.
    """
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"checkout-ping"}
    assert findings[0].details["missing_components"] == [
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/test-rg"
        "/providers/Microsoft.Insights/components/checkout-insights"
    ]


def test_a_test_with_no_link_tag_says_nothing_about_its_target(make_context):
    findings, _ = _findings(make_context)
    assert "hand-made" not in {f.resource_id for f in findings}


def test_the_cost_is_the_alert_noise_not_a_dollar_figure(make_context):
    findings, _ = _findings(make_context)
    assert findings[0].monthly_cost == 0.0
    assert "ignore the alert" in findings[0].details["note"]


def test_the_command_uses_core_az_rather_than_an_extension(make_context):
    """`az monitor app-insights` needs an extension a fresh machine lacks."""
    findings, _ = _findings(make_context)
    assert findings[0].remediation.startswith("az resource delete --ids ")
    assert "app-insights" not in findings[0].remediation
