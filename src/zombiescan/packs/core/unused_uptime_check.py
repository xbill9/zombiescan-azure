"""Uptime checks pointing at resources that no longer exist.

Cloud Monitoring bills uptime checks per check, per month, beyond a free
allowance. A check left behind by a deleted load balancer or instance keeps
running, keeps failing, and keeps billing -- and the alert it fires trains
everyone to ignore that alerting policy.

Only checks aimed at a Google Cloud resource this project can enumerate are
judged. One pointed at an external URL is left alone: nothing in the project
says whether that hostname is still meant to be up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-uptime-check"


def build_finding(ctx: ScanContext, config: dict[str, Any], target: str) -> Finding:
    check_id = gcp.last_segment(config.get("name"))
    resource = config.get("monitoredResource") or {}

    return Finding(
        check=CHECK_NAME,
        resource_id=check_id,
        resource_type="uptime-check",
        project=ctx.project,
        location=gcp.GLOBAL,
        reason=(
            f"Uptime check '{config.get('displayName', check_id)}' monitors instance "
            f"{target}, which no longer exists"
        ),
        # Cloud Monitoring's free allowance covers the first million check
        # executions a month, which most projects never exceed. The finding is
        # reported for the failing alerts it causes, not for a dollar figure.
        monthly_cost=0.0,
        remediation=(
            f"gcloud monitoring uptime delete {helpers.arg(check_id)} "
            f"--project={ctx.project} --quiet"
        ),
        details={
            "display_name": config.get("displayName"),
            "resource_type": resource.get("type"),
            "target": target,
            "period": config.get("period"),
            "selected_regions": config.get("selectedRegions") or [],
            "note": (
                "unpriced: uptime checks fall under a free monthly allowance most "
                "projects stay inside. This is reported for the alerts it keeps firing"
            ),
        },
    )


@check(CHECK_NAME, "Uptime checks monitoring deleted resources", apis=("monitoring", "compute"))
def unused_uptime_check(ctx: ScanContext) -> Iterator[Finding]:
    live_instance_ids = {
        str(instance["id"])
        for _scope, instance in gcp.aggregated(
            ctx.client("compute"), "instances", "instances", project=ctx.project
        )
        if instance.get("id")
    }

    for config in gcp.paginate(
        ctx.client("monitoring"),
        "projects.uptimeCheckConfigs",
        key="uptimeCheckConfigs",
        parent=ctx.parent,
    ):
        resource = config.get("monitoredResource") or {}
        if resource.get("type") != "gce_instance":
            # An external URL or a resource type this check cannot enumerate.
            continue
        instance_id = (resource.get("labels") or {}).get("instance_id")
        if not instance_id or instance_id in live_instance_ids:
            continue
        yield build_finding(ctx, config, instance_id)
