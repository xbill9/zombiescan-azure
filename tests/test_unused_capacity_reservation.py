"""Capacity reservations billing for slots no VM occupies."""

from __future__ import annotations

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.unused_capacity_reservation import (
    GROUP_TYPE,
    unused_capacity_reservation,
)

FIXTURE = load_fixture("unused_capacity_reservation")
D2S_V3 = 0.096 * 730
D4S_V5 = 0.192 * 730


def _findings(make_context):
    ctx, arm = make_context(
        {
            GROUP_TYPE: FIXTURE["groups"],
            "capacityReservationGroups/crg-launch": FIXTURE["launch_group"],
            "crg-launch/capacityReservations": FIXTURE["launch_reservations"],
            "capacityReservationGroups/crg-dr": FIXTURE["dr_group"],
            "crg-dr/capacityReservations": FIXTURE["dr_reservations"],
        }
    )
    return {f.resource_id: f for f in unused_capacity_reservation(ctx)}, arm


def test_a_fully_used_reservation_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert set(findings) == {"cr-idle", "cr-partial", "cr-failover"}


def test_every_unused_slot_bills_at_the_vm_rate(make_context):
    """Two reserved D2s_v3 with nothing on them cost what two running D2s_v3 do."""
    findings, _ = _findings(make_context)
    idle = findings["cr-idle"]
    assert idle.monthly_cost == 2 * D2S_V3
    assert idle.details["slots_unused"] == 2
    assert "all 2 reserved" in idle.reason


def test_associated_is_not_allocated(make_context):
    """Three VMs are associated with cr-partial and one is allocated. The two
    deallocated ones occupy nothing, so three of the four slots bill as unused."""
    findings, _ = _findings(make_context)
    partial = findings["cr-partial"]
    assert partial.details["slots_in_use"] == 1
    assert partial.monthly_cost == 3 * D4S_V5
    assert partial.details["in_use_from"].startswith("instance view")


def test_without_an_instance_view_the_associated_count_stands_in(make_context):
    """An upper bound on use, so the waste can be understated but not overstated."""
    findings, _ = _findings(make_context)
    failover = findings["cr-failover"]
    assert failover.details["slots_in_use"] == 1
    assert failover.monthly_cost == 2 * D2S_V3
    assert "upper bound" in failover.details["in_use_from"]


def test_the_group_is_read_with_its_instance_view(make_context):
    _, arm = _findings(make_context)
    group_reads = [path for path, args in arm.calls.items() if path.endswith("crg-launch")]
    assert group_reads
    assert arm.calls[group_reads[0]].get("$expand") == "instanceView"


def test_a_partly_used_reservation_is_shrunk_not_deleted(make_context):
    """Deleting it would give up the slot the running VM holds."""
    findings, _ = _findings(make_context)
    assert findings["cr-partial"].remediation == (
        "az capacity reservation update --capacity-reservation-group crg-launch "
        f"--name cr-partial --capacity 1 --resource-group test-rg --subscription {SUBSCRIPTION}"
    )
    assert findings["cr-idle"].remediation == (
        "az capacity reservation delete --capacity-reservation-group crg-launch "
        f"--name cr-idle --resource-group test-rg --subscription {SUBSCRIPTION} --yes"
    )
