"""Provisioned model deployments that served no requests in a week.

A provisioned deployment on Azure OpenAI or Azure AI Foundry reserves model
throughput in PTUs, and **every PTU bills every hour whether or not a request
arrives** -- $2.00 an hour regional, $1.10 data zone, $1.00 global, in eastus.
A 15-PTU regional deployment is $21,900 a month. A standard (pay-per-token)
deployment costs nothing idle and is not looked at.

Whether it is idle is read from Azure Monitor: ``ModelRequests`` on the
account over ``helpers.LOOKBACK_DAYS``, split by ``ModelDeploymentName`` so
one call covers every deployment on the account. A deployment that recorded
no requests has no series at all, which is read as zero.

Accounts are listed subscription-wide, and that list pages: ARM has been seen
to return an empty first page with a ``nextLink`` and the account on the
second. ``Arm.list`` follows it; a reader that stopped at page one would
report the subscription clean.

A PTU reservation, bought monthly or yearly, can cover the hourly charge. This
report prices at the hourly list rate and cannot see a reservation.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-provisioned-deployment"

ACCOUNT_TYPE = "Microsoft.CognitiveServices/accounts"
RESOURCE_TYPE = "Microsoft.CognitiveServices/accounts/deployments"

# The deployment SKUs that bill per PTU-hour. Standard, GlobalStandard,
# DataZoneStandard and the batch SKUs bill per token and cost nothing idle.
PROVISIONED = frozenset(
    {"ProvisionedManaged", "GlobalProvisionedManaged", "DataZoneProvisionedManaged"}
)

METRIC = "ModelRequests"
DIMENSION = "ModelDeploymentName"


def build_finding(ctx: ScanContext, account: dict[str, Any], deployment: dict[str, Any]) -> Finding:
    name = deployment["name"]
    arm_id = deployment.get("id") or ""
    account_name = account.get("name") or azure.name_of(arm_id.rsplit("/deployments/", 1)[0])
    group = account.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(account)
    sku = deployment.get("sku") or {}
    tier = sku.get("name") or ""
    ptus = int(sku.get("capacity") or 0)
    model = helpers.properties(deployment).get("model") or {}

    rate, approximate = ctx.pricing.rate(
        "ptu.month", region=azure.region_of(location), variant=tier
    )
    cost = rate * ptus

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{account_name}/{name}",
        resource_type="model-deployment",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"{ptus} PTU {tier} deployment of {model.get('name') or 'a model'} served no "
            f"requests in {helpers.LOOKBACK_DAYS} days; every PTU bills hourly regardless"
        ),
        monthly_cost=cost,
        remediation=helpers.az(
            f"az cognitiveservices account deployment delete --name {helpers.arg(account_name)} "
            f"--deployment-name {helpers.arg(name)}",
            ctx.subscription,
            group,
        ),
        approximate_cost=approximate,
        details={
            "account": account_name,
            "sku": tier,
            "ptus": ptus,
            "model": model.get("name"),
            "model_version": model.get("version"),
            "requests": 0,
            "lookback_days": helpers.LOOKBACK_DAYS,
            "tags": deployment.get("tags") or account.get("tags") or {},
            "note": (
                "hourly list rate per PTU. A PTU reservation may already cover it, "
                "and deleting the deployment does not refund one"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Provisioned model deployments serving nothing",
    providers="Microsoft.CognitiveServices",
)
def idle_provisioned_deployment(ctx: ScanContext) -> Iterator[Finding]:
    for account in ctx.list(ACCOUNT_TYPE):
        account_id = account.get("id")
        if not account_id:
            continue
        provisioned = [
            deployment
            for deployment in ctx.arm.list(f"{account_id}/deployments", RESOURCE_TYPE)
            if (deployment.get("sku") or {}).get("name") in PROVISIONED
        ]
        if not provisioned:
            continue
        # One metrics call per account, and only for accounts that have
        # something worth asking about.
        requests = helpers.metric_totals(ctx, account_id, METRIC, split_by=DIMENSION)
        for deployment in provisioned:
            if not requests.get(str(deployment.get("name") or "").lower()):
                yield build_finding(ctx, account, deployment)
