"""Availability tests pointing at something that is gone.

An Application Insights availability test calls a URL from several regions
every few minutes, for as long as it exists. When the thing it was watching is
deleted, the test keeps calling -- and keeps failing, and keeps firing whatever
alert rule is attached to it.

The test itself is effectively free. The cost is the alerts: a failing test
wired to an action group pages somebody, or fills a channel, every five
minutes forever, and the usual fix is for the humans to learn to ignore that
alert -- which is the expensive outcome, because the next real one is ignored
too.

A test is reported when the Application Insights component it belongs to no
longer exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-availability-test"

RESOURCE_TYPE = "Microsoft.Insights/webtests"

# The tag Application Insights writes on every web test, naming the component
# the test belongs to. It is the only link from the test back to its
# component: the test resource itself carries no reference.
COMPONENT_TAG_PREFIX = "hidden-link:"


def _linked_components(test: dict[str, Any]) -> list[str]:
    """The Application Insights components this test is attached to.

    Azure records the link as a *tag key* rather than as a property: a tag
    named ``hidden-link:/subscriptions/.../components/foo`` with the value
    "Resource". So the resource ids are in the keys, which is unusual enough
    that reading the values instead finds nothing and reports every test.
    """
    return [
        key[len(COMPONENT_TAG_PREFIX) :]
        for key in (test.get("tags") or {})
        if str(key).startswith(COMPONENT_TAG_PREFIX)
    ]


def build_finding(ctx: ScanContext, test: dict[str, Any], missing: list[str]) -> Finding:
    name = test["name"]
    arm_id = test.get("id") or ""
    group = test.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(test)
    properties = helpers.properties(test)

    gone = ", ".join(azure.name_of(component) for component in missing) or "its component"

    return Finding(
        check=CHECK_NAME,
        resource_id=properties.get("SyntheticMonitorId") or name,
        resource_type="availability-test",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"Availability test still runs against {gone}, which no longer exists, so "
            f"it fails on every run and fires whatever alert is attached to it"
        ),
        # The test's own cost is negligible. The cost is the alert noise, and
        # putting a dollar figure on that would be an invention.
        monthly_cost=0.0,
        # There is no `az monitor app-insights web-test delete` in core az --
        # it lives in an extension that is not installed by default, and a
        # generated plan that needs an extension installed first is a plan
        # that does not run.
        remediation=helpers.az_resource_delete(arm_id, ctx.subscription),
        details={
            "enabled": properties.get("Enabled"),
            "frequency_seconds": properties.get("Frequency"),
            "test_locations": [
                location.get("Id") for location in properties.get("Locations") or []
            ],
            "missing_components": missing,
            "tags": test.get("tags") or {},
            "note": (
                "no meaningful charge. Reported because a test that can only fail "
                "trains everyone to ignore the alert it fires"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Availability tests watching a deleted resource",
    providers="Microsoft.Insights",
)
def unused_availability_test(ctx: ScanContext) -> Iterator[Finding]:
    live = helpers.arm_ids(
        ctx.graph("Resources | where type =~ 'microsoft.insights/components' | project id")
    )
    for test in ctx.list(RESOURCE_TYPE):
        linked = _linked_components(test)
        # A test with no link tag was made by hand rather than by the portal,
        # and there is nothing to say its target is gone.
        if not linked:
            continue
        missing = [component for component in linked if component.lower() not in live]
        if missing:
            yield build_finding(ctx, test, missing)
