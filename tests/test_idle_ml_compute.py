"""ML compute instances that never stop, and clusters held above zero."""

from __future__ import annotations

import json

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.idle_ml_compute import WORKSPACE_TYPE, idle_ml_compute

FIXTURE = load_fixture("idle_ml_compute")


def _findings(make_context):
    ctx, _ = make_context(
        {WORKSPACE_TYPE: FIXTURE["workspaces"], "ws-research/computes": FIXTURE["computes"]}
    )
    return {f.resource_id: f for f in idle_ml_compute(ctx)}


def test_only_compute_that_will_not_stop_on_its_own_is_reported(make_context):
    """An idle shutdown, a stop schedule, minimum zero or running jobs all clear it."""
    assert set(_findings(make_context)) == {"ws-research/ci-forever", "ws-research/gpu-warm"}


def test_an_ml_size_in_capitals_is_priced(make_context):
    """ML spells it STANDARD_DS3_V2; the price table spells it Standard_DS3_v2."""
    finding = _findings(make_context)["ws-research/ci-forever"]
    assert finding.monthly_cost == 0.229 * 730
    assert not finding.approximate_cost


def test_a_cluster_is_priced_at_the_idle_nodes_its_minimum_holds(make_context):
    finding = _findings(make_context)["ws-research/gpu-warm"]
    assert finding.monthly_cost == 2 * 3.06 * 730
    assert finding.details["billed_nodes"] == 2


def test_the_instance_is_stopped_through_core_az(make_context):
    finding = _findings(make_context)["ws-research/ci-forever"]
    assert finding.remediation.startswith("az resource invoke-action --action stop --ids ")
    assert finding.remediation.endswith(f"--subscription {SUBSCRIPTION}")


def test_the_cluster_command_keeps_its_maximum_and_timeout(make_context):
    finding = _findings(make_context)["ws-research/gpu-warm"]
    body = finding.remediation.split("--body ")[1].split(" --subscription")[0].strip("'")
    assert json.loads(body) == {
        "properties": {
            "properties": {
                "scaleSettings": {
                    "minNodeCount": 0,
                    "maxNodeCount": 4,
                    "nodeIdleTimeBeforeScaleDown": "PT120S",
                }
            }
        }
    }
