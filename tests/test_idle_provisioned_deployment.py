"""Provisioned model deployments that served nothing in a week."""

from __future__ import annotations

from tests.conftest import SUBSCRIPTION, load_fixture
from zombiescan.packs.core.idle_provisioned_deployment import (
    ACCOUNT_TYPE,
    idle_provisioned_deployment,
)

FIXTURE = load_fixture("idle_provisioned_deployment")


def _findings(make_context):
    ctx, arm = make_context(
        {
            ACCOUNT_TYPE: FIXTURE["accounts"],
            "accounts/acct-ptu/deployments": FIXTURE["ptu_deployments"],
            "accounts/acct-paygo/deployments": FIXTURE["paygo_deployments"],
            "accounts/acct-ptu/providers/Microsoft.Insights/metrics": FIXTURE["ptu_requests"],
        }
    )
    return {f.resource_id: f for f in idle_provisioned_deployment(ctx)}, arm


def test_only_a_provisioned_deployment_with_no_requests_is_reported(make_context):
    """ptu-busy served 1,200 requests; chat is pay-per-token and costs nothing idle."""
    findings, _ = _findings(make_context)
    assert set(findings) == {"acct-ptu/ptu-idle"}


def test_every_ptu_is_priced_by_the_hour(make_context):
    findings, _ = _findings(make_context)
    idle = findings["acct-ptu/ptu-idle"]
    assert idle.monthly_cost == 15 * 2.0 * 730
    assert idle.details["ptus"] == 15
    assert "served no requests in 7 days" in idle.reason


def test_one_metrics_call_per_account_split_by_deployment(make_context):
    _, arm = _findings(make_context)
    metric_calls = {path: args for path, args in arm.calls.items() if path.endswith("/metrics")}
    assert len(metric_calls) == 1
    (args,) = metric_calls.values()
    assert args["metricnames"] == "ModelRequests"
    assert args["$filter"] == "ModelDeploymentName eq '*'"
    assert args["timespan"] == "P7D"


def test_an_account_with_no_provisioned_deployment_is_not_asked_for_metrics(make_context):
    _, arm = _findings(make_context)
    assert not any("acct-paygo/providers" in path for path in arm.calls)


def test_the_command_names_account_and_deployment(make_context):
    findings, _ = _findings(make_context)
    assert findings["acct-ptu/ptu-idle"].remediation == (
        "az cognitiveservices account deployment delete --name acct-ptu "
        f"--deployment-name ptu-idle --resource-group test-rg --subscription {SUBSCRIPTION}"
    )


def test_the_metrics_provider_is_declared():
    """The request count comes from Microsoft.Insights, so the engine must check
    it is registered before trusting a zero."""
    from zombiescan.registry import CHECKS

    assert "Microsoft.Insights" in CHECKS["idle-provisioned-deployment"].providers
