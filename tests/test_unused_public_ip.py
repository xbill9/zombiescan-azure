"""Public IP addresses attached to nothing."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_public_ip import RESOURCE_TYPE, unused_public_ip


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("unused_public_ip")})
    return list(unused_public_ip(ctx)), arm


def test_an_address_on_a_nic_or_a_nat_gateway_is_in_use(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"lb-frontend-old", "legacy-basic"}


def test_a_standard_static_address_is_billed_whether_or_not_it_is_attached(make_context):
    """The Azure shape: idle costs the same as in use, so only reservation matters."""
    findings, _ = _findings(make_context)
    address = next(f for f in findings if f.resource_id == "lb-frontend-old")
    assert address.monthly_cost == 0.005 * 730
    assert "same hourly rate as an address in use" in address.reason


def test_a_dynamic_basic_address_costs_nothing_while_detached(make_context):
    """It is still reported: Basic addresses are retired and must be migrated."""
    findings, _ = _findings(make_context)
    basic = next(f for f in findings if f.resource_id == "legacy-basic")
    assert basic.monthly_cost == 0.0
    assert "retired" in basic.reason


def test_the_finding_warns_that_the_address_cannot_be_got_back(make_context):
    findings, _ = _findings(make_context)
    assert "permanently" in findings[0].details["note"]


def test_the_command_takes_no_yes_flag(make_context):
    """`az network public-ip delete` does not prompt, and rejects --yes."""
    findings, _ = _findings(make_context)
    assert "--yes" not in findings[0].remediation
