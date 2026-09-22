"""Security groups protecting nothing."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_nsg import RESOURCE_TYPE, unused_nsg


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("unused_nsg")})
    return list(unused_nsg(ctx)), arm


def test_an_nsg_on_a_subnet_is_in_force(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"legacy-web-nsg"}


def test_the_finding_is_hygiene_not_spend(make_context):
    findings, _ = _findings(make_context)
    assert findings[0].monthly_cost == 0.0
    assert "no charge" in findings[0].details["note"]


def test_the_rules_that_are_not_in_force_are_named(make_context):
    """ "Two rules, none of them applied" is the fact worth acting on."""
    findings, _ = _findings(make_context)
    assert findings[0].details["custom_rules"] == 2
    assert findings[0].details["rule_names"] == ["allow-http", "allow-ssh-from-office"]
    assert "none of them is in force" in findings[0].reason
