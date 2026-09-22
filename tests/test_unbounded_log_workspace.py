"""Log Analytics workspaces with no ceiling on what they accept."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.unbounded_log_workspace import (
    CHECK_NAME,
    RESOURCE_TYPE,
    unbounded_log_workspace,
)
from zombiescan.registry import CHECKS


def _findings(make_context):
    ctx, arm = make_context({RESOURCE_TYPE: load_fixture("unbounded_log_workspace")})
    return list(unbounded_log_workspace(ctx)), arm


def test_a_capped_workspace_is_a_deliberate_choice(make_context):
    findings, _ = _findings(make_context)
    assert "capped-logs" not in {f.resource_id for f in findings}


def test_short_retention_alone_is_not_the_shape(make_context):
    """Both together -- no cap and long retention -- is what surprises people."""
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"central-logs"}


def test_it_is_reported_as_growth_rather_than_priced(make_context):
    """The listing does not say how much has been ingested, and inventing it would lie."""
    findings, _ = _findings(make_context)
    assert findings[0].monthly_cost == 0.0
    assert "unpriced" in findings[0].details["note"]


def test_the_rates_that_would_apply_are_still_reported(make_context):
    findings, _ = _findings(make_context)
    details = findings[0].details
    assert details["usd_per_gb_ingested"] == 2.30
    assert details["usd_per_gb_month_retained"] == 0.10
    assert details["billable_retention_days"] == 730 - 31


def test_the_check_refuses_to_set_a_cap_and_says_why(make_context):
    """A cap set too low drops the logs an incident is investigated from."""
    spec = CHECKS[CHECK_NAME]
    assert spec.uncleanable
    assert "drops the logs" in spec.uncleanable
