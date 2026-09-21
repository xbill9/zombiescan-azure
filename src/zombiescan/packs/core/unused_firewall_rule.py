"""Firewall rules that can never match anything.

A firewall rule costs nothing to keep. It is reported because a VPC full of
rules nobody can account for is how a rule that should have been deleted stays
open, and because the rules left behind by a deleted workload are exactly the
ones nobody dares remove without evidence.

Two cases are reported, both provable from the rule itself plus the instances
in its network: a rule that is explicitly disabled, and a rule targeting a
network tag that no instance in that network carries.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-firewall-rule"


def build_finding(ctx: ScanContext, rule: dict[str, Any], why: str) -> Finding:
    name = rule["name"]
    network = gcp.last_segment(rule.get("network"))
    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="firewall-rule",
        project=ctx.project,
        location=gcp.GLOBAL,
        reason=why,
        # Firewall rules are free. The finding is hygiene: it is reported so
        # the rule can be accounted for, not because it is on the bill.
        monthly_cost=0.0,
        remediation=(
            f"gcloud compute firewall-rules delete {helpers.arg(name)} "
            f"--project={ctx.project} --quiet"
        ),
        details={
            "network": network,
            "direction": rule.get("direction"),
            "disabled": bool(rule.get("disabled")),
            "priority": rule.get("priority"),
            "target_tags": rule.get("targetTags") or [],
            "target_service_accounts": rule.get("targetServiceAccounts") or [],
            "source_ranges": rule.get("sourceRanges") or [],
        },
    )


def _tags_in_use(ctx: ScanContext) -> dict[str, set[str]]:
    """Network tags carried by at least one instance, grouped by network.

    Grouped by network because a tag is only meaningful inside the network the
    rule applies to: the same tag name in another VPC does not make the rule
    live.
    """
    tags: dict[str, set[str]] = {}
    for _scope, instance in gcp.aggregated(
        ctx.client("compute"), "instances", "instances", project=ctx.project
    ):
        carried = set((instance.get("tags") or {}).get("items") or [])
        if not carried:
            continue
        for interface in instance.get("networkInterfaces") or []:
            network = gcp.last_segment(interface.get("network"))
            if network:
                tags.setdefault(network, set()).update(carried)
    return tags


@check(CHECK_NAME, "Firewall rules matching nothing", apis="compute")
def unused_firewall_rule(ctx: ScanContext) -> Iterator[Finding]:
    in_use = _tags_in_use(ctx)
    for rule in gcp.paginate(ctx.client("compute"), "firewalls", project=ctx.project):
        network = gcp.last_segment(rule.get("network"))
        if rule.get("disabled"):
            yield build_finding(
                ctx, rule, f"Firewall rule is disabled, so it matches nothing in '{network}'"
            )
            continue

        target_tags = rule.get("targetTags") or []
        if not target_tags:
            # No target tags means the rule applies to every instance in the
            # network. That is not unused, it is broad.
            continue
        if in_use.get(network, set()) & set(target_tags):
            continue
        yield build_finding(
            ctx,
            rule,
            f"Firewall rule targets tag(s) {', '.join(sorted(target_tags))}, which no "
            f"instance in '{network}' carries",
        )
