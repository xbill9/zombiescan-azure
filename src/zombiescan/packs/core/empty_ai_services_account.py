"""Azure OpenAI and AI Services accounts with no model deployed.

An ``OpenAI`` or ``AIServices`` account exists to host model deployments. One
with none, and no Foundry project either, is serving nothing: every request
to it would fail for want of a model. The S0 account itself carries no base
fee, so this is hygiene rather than a bill -- but it holds an endpoint, keys
and network rules that read as something in use.

An account with a project but no deployment is not reported: a project can
hold agents, evaluations and connections to models hosted elsewhere, and
deleting the account deletes the project with it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "empty-ai-services-account"

RESOURCE_TYPE = "Microsoft.CognitiveServices/accounts"

# The account kinds whose purpose is hosting deployments. Speech, Vision,
# Language and the rest have no deployments to count.
MODEL_HOSTS = frozenset({"openai", "aiservices"})


def build_finding(ctx: ScanContext, account: dict[str, Any]) -> Finding:
    name = account["name"]
    arm_id = account.get("id") or ""
    group = account.get("resourceGroup") or azure.resource_group_of(arm_id)
    properties = helpers.properties(account)
    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="ai-services-account",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=helpers.location_of(account),
        reason=f"{account.get('kind')} account has no model deployment and no project",
        monthly_cost=0.0,
        remediation=helpers.az(
            f"az cognitiveservices account delete --name {helpers.arg(name)}",
            ctx.subscription,
            group,
        ),
        details={
            "kind": account.get("kind"),
            "sku": (account.get("sku") or {}).get("name"),
            "endpoint": properties.get("endpoint"),
            "public_network_access": properties.get("publicNetworkAccess"),
            "tags": account.get("tags") or {},
            "note": (
                "an account with no deployment is not billed. Deleting it is recoverable "
                "for 48 hours through Cognitive Services soft-delete"
            ),
        },
    )


@check(
    CHECK_NAME,
    "AI Services accounts with no model deployed",
    providers="Microsoft.CognitiveServices",
)
def empty_ai_services_account(ctx: ScanContext) -> Iterator[Finding]:
    for account in ctx.list(RESOURCE_TYPE):
        account_id = account.get("id")
        if not account_id or str(account.get("kind") or "").lower() not in MODEL_HOSTS:
            continue
        if next(iter(ctx.arm.list(f"{account_id}/deployments", RESOURCE_TYPE)), None):
            continue
        if next(iter(ctx.arm.list(f"{account_id}/projects", RESOURCE_TYPE)), None):
            continue
        yield build_finding(ctx, account)
