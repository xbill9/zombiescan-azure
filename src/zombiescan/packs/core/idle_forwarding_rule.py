"""Load balancer forwarding rules with no backend to send traffic to.

Google bills a load balancer by its forwarding rules, at a flat hourly minimum
covering the first five. That minimum is charged whether or not the backend
service behind the rule has any instance groups or network endpoint groups in
it, so a load balancer whose backends were all removed keeps billing for
nothing.

Both regional and global forwarding rules are checked; the global ones front
the external HTTP(S) load balancers, which are the expensive ones to leave up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-forwarding-rule"


def build_finding(
    ctx: ScanContext, location: str, rule: dict[str, Any], target_name: str, why: str
) -> Finding:
    name = rule["name"]
    price, approximate = ctx.pricing.rate("forwarding_rule.month", region=gcp.region_of(location))
    is_global = location == gcp.GLOBAL
    scope_flag = "--global" if is_global else f"--region={location}"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="forwarding-rule",
        project=ctx.project,
        location=location,
        reason=f"Forwarding rule bills the load balancer minimum but {why}",
        monthly_cost=price,
        remediation=(
            f"gcloud compute forwarding-rules delete {helpers.arg(name)} {scope_flag} "
            f"--project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "ip_address": rule.get("IPAddress"),
            "ip_protocol": rule.get("IPProtocol"),
            "port_range": rule.get("portRange"),
            "load_balancing_scheme": rule.get("loadBalancingScheme"),
            "target": target_name or None,
            "network_tier": rule.get("networkTier"),
            "note": "forwarding rule minimum only; data processing is not included",
        },
    )


def _empty_backend_services(ctx: ScanContext) -> set[str]:
    """Backend services with no instance group or endpoint group attached.

    These are the ones a forwarding rule can point at while still having
    nowhere to send a request.
    """
    empty = set()
    for _scope, service in gcp.aggregated(
        ctx.client("compute"), "backendServices", "backendServices", project=ctx.project
    ):
        if not service.get("backends"):
            empty.add(service["name"])
    return empty


@check(CHECK_NAME, "Load balancers with no backends", apis="compute")
def idle_forwarding_rule(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("compute")
    empty = _empty_backend_services(ctx)

    def examine(location: str, rule: dict[str, Any]) -> Finding | None:
        target = rule.get("target") or rule.get("backendService")
        target_name = gcp.last_segment(target)
        if not target:
            # A rule pointing at nothing at all cannot serve a request.
            return build_finding(ctx, location, rule, "", "points at no target")
        if target_name in empty:
            return build_finding(
                ctx,
                location,
                rule,
                target_name,
                f"its backend service '{target_name}' has no backends",
            )
        return None

    for scope, rule in gcp.aggregated(
        client, "forwardingRules", "forwardingRules", project=ctx.project
    ):
        finding = examine(gcp.location_from_scope(scope), rule)
        if finding is not None:
            yield finding

    for rule in gcp.paginate(client, "globalForwardingRules", project=ctx.project):
        finding = examine(gcp.GLOBAL, rule)
        if finding is not None:
            yield finding
