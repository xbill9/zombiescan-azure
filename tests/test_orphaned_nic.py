"""Network interfaces with no VM, and what they block."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.orphaned_nic import RESOURCE_TYPE, orphaned_nic


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("orphaned_nic")})
    return list(orphaned_nic(ctx)), arm


def test_a_nic_on_a_vm_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert "web-01-nic" not in {f.resource_id for f in findings}


def test_a_private_endpoint_nic_has_no_vm_and_is_emphatically_in_use(make_context):
    """It has no `virtualMachine`, so the naive test would report it."""
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"web-01-nic-old"}


def test_a_nic_is_not_billed_and_says_so(make_context):
    findings, _ = _findings(make_context)
    assert findings[0].monthly_cost == 0.0
    assert "a NIC is not billed" in findings[0].details["note"]


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
