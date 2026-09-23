"""Network interfaces with no VM, and what they block."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.orphaned_nic import RESOURCE_TYPE, orphaned_nic


def _findings(make_context):
    ctx, arm = make_context(
        {
            RESOURCE_TYPE: load_fixture("orphaned_nic"),
            "Microsoft.Network/publicIPAddresses": load_fixture("held_public_ips"),
        }
    )
    return list(orphaned_nic(ctx)), arm


def test_a_nic_on_a_vm_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "web-01-nic" not in {f.resource_id for f in findings}


def test_a_private_endpoint_nic_has_no_vm_and_is_emphatically_in_use(make_context):
    """It has no `virtualMachine`, so the naive test would report it."""
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"web-01-nic-old"}


def test_a_nic_is_priced_at_the_public_ips_it_holds(make_context):
    """unused-public-ip skips an address whose ipConfiguration names this NIC,
    so this finding is the only one that can carry its cost."""
    findings, _ = _findings(make_context)
    assert findings[0].monthly_cost == 0.005 * 730
    assert [ip["name"] for ip in findings[0].details["public_ips"]] == ["lb-frontend-old"]
    assert "a NIC is not billed" in findings[0].details["note"]
    assert "/month" in findings[0].reason


def test_a_nic_holding_no_address_costs_nothing(make_context):
    ctx, _ = make_context({RESOURCE_TYPE: load_fixture("orphaned_nic")})
    findings = list(orphaned_nic(ctx))
    assert findings[0].monthly_cost == 0.0
    assert findings[0].details["public_ips"] == []


def test_the_finding_names_what_the_nic_is_pinning(make_context):
    """This is the reason the check exists: it unblocks the priced cleanups."""
    findings, _ = _findings(make_context)
    blocks = findings[0].details["blocks"]
    assert blocks["public_ips"] == ["lb-frontend-old"]
    assert blocks["subnets"] == [
        "/subscriptions/00000000-1111-2222-3333-444444444444/resourceGroups/test-rg"
        "/providers/Microsoft.Network/virtualNetworks/prod-vnet/subnets/web"
    ]
    assert "public IP address(es)" in findings[0].reason
