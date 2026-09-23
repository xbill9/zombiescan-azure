"""Stopped VMs, and the difference between stopped and deallocated."""

from __future__ import annotations

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.deallocated_vm import deallocated_vm

FIXTURE = load_fixture("deallocated_vm")


def _findings(make_context):
    ctx, arm = make_context(
        {
            "Microsoft.Compute/disks": FIXTURE["disks"],
            "Microsoft.Network/publicIPAddresses": load_fixture("held_public_ips"),
        },
        graph={"microsoft.compute/virtualmachines": FIXTURE["graph"]},
    )
    return list(deallocated_vm(ctx)), arm


def test_running_vms_are_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"batch-runner", "forgotten-lab"}


def test_a_deallocated_vm_is_priced_at_its_disks_and_public_ip(make_context):
    """128 GiB Premium is a P10 ($20), 32 GiB Standard HDD an S4 ($1.50), and a
    Standard static address $3.65 whether the VM runs or not."""
    findings, _ = _findings(make_context)
    vm = next(f for f in findings if f.resource_id == "batch-runner")
    assert vm.monthly_cost == 21.50 + 0.005 * 730
    assert [ip["name"] for ip in vm.details["public_ips"]] == ["batch-runner-ip"]
    assert "1 public IP(s)" in vm.reason
    assert [d["role"] for d in vm.details["disks"]] == ["os", "data"]
    assert [d["billed_tier"] for d in vm.details["disks"]] == ["P10 LRS", "S4 LRS"]


def test_stopped_and_deallocated_are_told_apart(make_context):
    """The costly mistake is 'stopped', where compute is still reserved."""
    findings, _ = _findings(make_context)
    stopped = next(f for f in findings if f.resource_id == "forgotten-lab")
    deallocated = next(f for f in findings if f.resource_id == "batch-runner")

    assert "not deallocated" in stopped.reason
    assert "still reserved" in stopped.reason
    assert "compute is still" not in deallocated.reason
    assert "not billed for" in stopped.details["note"]


def test_the_disks_are_left_behind_so_the_step_is_reversible(make_context):
    findings, _ = _findings(make_context)
    vm = next(f for f in findings if f.resource_id == "batch-runner")
    assert vm.remediation == (
        f"az vm delete --name batch-runner --resource-group test-rg "
        f"--subscription {SUBSCRIPTION} --yes"
    )
    # --force-deletion would take the disks with it, and the point of leaving
    # them is that unattached-disk reports them with a snapshot-first plan.
    assert "--force-deletion" not in vm.remediation


def test_a_vm_with_no_public_ip_is_priced_at_its_disks_alone(make_context):
    findings, _ = _findings(make_context)
    vm = next(f for f in findings if f.resource_id == "forgotten-lab")
    assert vm.details["public_ips"] == []
    assert "public IP" not in vm.reason


def test_the_check_needs_the_network_provider_too(make_context):
    """Its addresses are read from Microsoft.Network; an unregistered provider
    must stop the check rather than price the VM without them."""
    from zombiescan.registry import CHECKS

    assert "Microsoft.Network" in CHECKS["deallocated-vm"].providers


def test_a_disk_the_scan_cannot_see_is_skipped_rather_than_guessed(make_context):
    """An ephemeral OS disk has no managed disk to price, and costs nothing."""
    ctx, _ = make_context(
        {"Microsoft.Compute/disks": {"value": []}},
        graph={"microsoft.compute/virtualmachines": FIXTURE["graph"]},
    )
    findings = list(deallocated_vm(ctx))
    assert all(f.monthly_cost == 0.0 for f in findings)
    assert all(f.details["disks"] == [] for f in findings)
