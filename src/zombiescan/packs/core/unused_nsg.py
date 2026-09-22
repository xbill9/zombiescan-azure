"""Network security groups attached to nothing.

An NSG costs nothing. It is reported because an NSG attached to no subnet and
no network interface is a rule set nobody is reading -- and a rule set nobody
is reading is worse than no rule set, because the next person to look assumes
it is in force. A subnet whose NSG was detached is wide open while its rules
sit in the portal looking authoritative.

This is hygiene, not spend, and it is priced at zero honestly rather than
given an invented cost to make it rank.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-nsg"

RESOURCE_TYPE = "Microsoft.Network/networkSecurityGroups"


def build_finding(ctx: ScanContext, group_resource: dict[str, Any]) -> Finding:
    name = group_resource["name"]
    arm_id = group_resource.get("id") or ""
    group = group_resource.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(group_resource)
    properties = helpers.properties(group_resource)
    rules = properties.get("securityRules") or []

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="network-security-group",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"Network security group carries {len(rules)} custom rule(s) and is attached "
            f"to no subnet and no network interface, so none of them is in force"
        ),
        monthly_cost=0.0,
        remediation=helpers.az(
            f"az network nsg delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        details={
            "custom_rules": len(rules),
            "rule_names": [rule.get("name") for rule in rules],
            "flow_logs": len(properties.get("flowLogs") or []),
            "tags": group_resource.get("tags") or {},
            "note": (
                "no charge. Reported because detached rules read as protection that is not there"
            ),
        },
    )


@check(CHECK_NAME, "Security groups protecting nothing", providers="Microsoft.Network")
def unused_nsg(ctx: ScanContext) -> Iterator[Finding]:
    for nsg in ctx.list(RESOURCE_TYPE):
        properties = helpers.properties(nsg)
        if properties.get("subnets") or properties.get("networkInterfaces"):
            continue
        yield build_finding(ctx, nsg)
