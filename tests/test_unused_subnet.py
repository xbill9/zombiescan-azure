"""Subnets reserving a range against nothing."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_subnet import RESOURCE_TYPE, unused_subnet


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("unused_subnet")})
    return list(unused_subnet(ctx)), arm


def test_a_subnet_with_ip_configurations_is_in_use(make_context):
    findings, _ = _findings(make_context)
    assert "prod-vnet/web" not in {f.resource_id for f in findings}


def test_a_delegated_subnet_is_in_use_despite_having_no_ip_configurations(make_context):
    """A delegation hands address management to a service, which then uses it."""
    findings, _ = _findings(make_context)
    assert "prod-vnet/functions" not in {f.resource_id for f in findings}


def test_azures_own_reserved_subnet_names_are_left_alone(make_context):
    """GatewaySubnet exists to be empty until a gateway lands in it."""
    findings, _ = _findings(make_context)
    assert "prod-vnet/GatewaySubnet" not in {f.resource_id for f in findings}


def test_only_the_genuinely_empty_subnet_is_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"prod-vnet/retired-batch"}


def test_the_finding_counts_the_addresses_azure_actually_offers(make_context):
    """A /24 offers 251, not 256: Azure reserves five in every subnet."""
    findings, _ = _findings(make_context)
    assert findings[0].details["usable_addresses"] == 251
    assert "251 usable address(es)" in findings[0].reason


def test_the_command_names_the_network_the_subnet_belongs_to(make_context):
    """A subnet name is unique only inside its virtual network."""
    findings, _ = _findings(make_context)
    assert "--vnet-name prod-vnet" in findings[0].remediation
    assert "--name retired-batch" in findings[0].remediation
