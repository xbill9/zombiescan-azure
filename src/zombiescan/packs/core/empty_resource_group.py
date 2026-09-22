"""Resource groups holding nothing.

A resource group costs nothing and is reported because it is the cheapest
finding to act on in the whole catalog: there is, by definition, nothing
inside it to check before deleting it, and nothing that can break.

Empty groups accumulate faster on Azure than on any other cloud because
almost every managed service creates one. Deleting an AKS cluster leaves its
node resource group; deleting a VM leaves the group it was deployed into;
every abandoned quickstart leaves one behind. They do not appear in a cost
report, because they cost nothing, so nothing ever prompts anyone to remove
them -- and a subscription with two hundred of them is genuinely harder to
work in.

Google Cloud has no equivalent: a project is the unit a resource lives in, and
an empty one is a project you delete rather than a container inside one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "empty-resource-group"

RESOURCE_TYPE = "Microsoft.Resources/resourceGroups"

# A group Azure manages on another resource's behalf -- an AKS node group is
# the common one. It is empty because its cluster has scaled to zero, not
# because it is abandoned, and deleting it breaks the cluster.
MANAGED_BY = "managedBy"


def build_finding(ctx: ScanContext, group: dict[str, Any]) -> Finding:
    name = group["name"]
    arm_id = group.get("id") or ""
    location = helpers.location_of(group)

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="resource-group",
        subscription=ctx.subscription,
        # A resource group is its own group. Saying so keeps the field
        # meaningful rather than empty for this one check.
        resource_group=name,
        arm_id=arm_id,
        location=location,
        reason="Resource group contains no resources",
        monthly_cost=0.0,
        remediation=helpers.az(f"az group delete --name {helpers.arg(name)}", ctx.subscription),
        details={
            "tags": group.get("tags") or {},
            "note": (
                "no charge. Reported because nothing else will ever prompt you to "
                "remove it, and an empty group is the only finding here with nothing "
                "inside it to check first"
            ),
        },
    )


@check(CHECK_NAME, "Resource groups holding nothing", providers="Microsoft.Resources")
def empty_resource_group(ctx: ScanContext) -> Iterator[Finding]:
    # Counting resources per group is exactly what Resource Graph is for: one
    # query returns the count for every group, where listing each group's
    # contents would be one call per group.
    populated = {
        str(row.get("resourceGroup") or "").lower()
        for row in ctx.graph("Resources | summarize resources = count() by resourceGroup")
        if int(row.get("resources") or 0) > 0
    }

    for group in ctx.list(RESOURCE_TYPE):
        if str(group.get("name") or "").lower() in populated:
            continue
        # Azure manages some groups on another resource's behalf. An AKS node
        # group scaled to zero is empty and deleting it breaks the cluster.
        if group.get(MANAGED_BY):
            continue
        yield build_finding(ctx, group)
