"""Dedicated hosts with nothing running on them."""

from __future__ import annotations

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.idle_dedicated_host import GROUP_TYPE, idle_dedicated_host

FIXTURE = load_fixture("idle_dedicated_host")


def _findings(make_context):
    ctx, arm = make_context(
        {
            GROUP_TYPE: FIXTURE["groups"],
            "hostGroups/hg-prod/hosts": FIXTURE["hosts_prod"],
            "hostGroups/hg-lab/hosts": FIXTURE["hosts_lab"],
        }
    )
    return list(idle_dedicated_host(ctx)), arm


def test_a_host_running_vms_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"host-idle", "host-new-family"}


def test_hosts_are_listed_in_every_host_group(make_context):
    """There is no subscription-wide list of hosts, only one per group."""
    _, arm = _findings(make_context)
    listed = [path for kind, path in arm.call_log if kind == "list"]
    assert any(path.endswith("hostGroups/hg-prod/hosts") for path in listed)
    assert any(path.endswith("hostGroups/hg-lab/hosts") for path in listed)


def test_an_idle_host_is_priced_at_the_full_host_rate(make_context):
    """A host bills per host whatever runs on it: $4.225 an hour for DSv3-Type3."""
    findings, _ = _findings(make_context)
    host = next(f for f in findings if f.resource_id == "host-idle")
    assert host.monthly_cost == 4.225 * 730
    assert not host.approximate_cost
    assert "billed per host regardless" in host.reason
    assert host.details["host_group"] == "hg-prod"


def test_an_unknown_host_sku_is_unpriced_rather_than_guessed(make_context):
    """Host rates run from under $1 to over $50 an hour; a guess would be wrong by that."""
    findings, _ = _findings(make_context)
    host = next(f for f in findings if f.resource_id == "host-new-family")
    assert host.monthly_cost == 0.0
    assert host.approximate_cost


def test_the_command_names_the_host_group(make_context):
    findings, _ = _findings(make_context)
    host = next(f for f in findings if f.resource_id == "host-idle")
    assert host.remediation == (
        f"az vm host delete --host-group hg-prod --name host-idle --resource-group test-rg "
        f"--subscription {SUBSCRIPTION} --yes"
    )


def test_licences_are_excluded_and_the_finding_says_so(make_context):
    findings, _ = _findings(make_context)
    assert "licences" in findings[0].details["note"]
