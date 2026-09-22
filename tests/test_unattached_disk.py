"""Unattached managed disks, and the tier-not-gigabyte pricing that goes with them."""

from __future__ import annotations

from tests.conftest import RESOURCE_GROUP, SUBSCRIPTION, load_fixture
from zombiescan.packs.core.unattached_disk import RESOURCE_TYPE, unattached_disk


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("unattached_disk")})
    return list(unattached_disk(ctx)), arm


def test_only_disks_nothing_holds_are_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"orphan-data", "tiny-scratch", "v2-orphan"}


def test_an_attached_disk_is_not_waste(make_context):
    findings, _ = _findings(make_context)
    assert "web-01-os" not in {f.resource_id for f in findings}


def test_a_disk_owned_by_something_other_than_a_vm_is_not_waste(make_context):
    """`diskState` says Unattached while `managedBy` still names an owner.

    A disk pool member reports exactly this, and deleting it would break the
    pool rather than save anything.
    """
    findings, _ = _findings(make_context)
    assert "pool-member" not in {f.resource_id for f in findings}


def test_a_disk_is_priced_by_its_tier_not_by_its_size(make_context):
    """128 GiB Premium is a P10, and a P10 has one price.

    Pricing per GiB -- the right answer on every other cloud -- would give
    128 x something here and be wrong by orders of magnitude.
    """
    findings, _ = _findings(make_context)
    disk = next(f for f in findings if f.resource_id == "orphan-data")
    assert disk.details["billed_tier"] == "P10 LRS"
    assert disk.monthly_cost == 20.00


def test_a_small_disk_is_billed_at_the_rung_it_lands_on(make_context):
    """4 GiB Premium is a P1 at $0.60, not four gigabytes of anything."""
    findings, _ = _findings(make_context)
    disk = next(f for f in findings if f.resource_id == "tiny-scratch")
    assert disk.details["billed_tier"] == "P1 LRS"
    assert disk.monthly_cost == 0.60


def test_premium_v2_is_the_one_family_billed_per_gib(make_context):
    findings, _ = _findings(make_context)
    disk = next(f for f in findings if f.resource_id == "v2-orphan")
    assert disk.details["billed_tier"] is None
    assert disk.monthly_cost == 200 * 0.08
    assert "provisioned IOPS" in disk.details["note"]


def test_the_finding_carries_what_the_command_needs(make_context):
    findings, _ = _findings(make_context)
    disk = next(f for f in findings if f.resource_id == "orphan-data")
    assert disk.resource_group == RESOURCE_GROUP
    assert disk.subscription == SUBSCRIPTION
    assert disk.arm_id.endswith("/providers/Microsoft.Compute/disks/orphan-data")
    assert disk.location == "eastus"


def test_the_remediation_names_its_subscription_and_group(make_context):
    findings, _ = _findings(make_context)
    disk = next(f for f in findings if f.resource_id == "orphan-data")
    assert disk.remediation == (
        "az disk delete --name orphan-data "
        f"--resource-group {RESOURCE_GROUP} --subscription {SUBSCRIPTION} --yes"
    )


def test_one_call_covers_every_resource_group(make_context):
    """The whole point of the subscription-wide list: no per-group fan-out."""
    _, arm = _findings(make_context)
    assert len(arm.call_log) == 1
    assert arm.call_log[0] == ("list", f"/subscriptions/{SUBSCRIPTION}/providers/{RESOURCE_TYPE}")
