"""AI Services accounts hosting no model and no project."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.empty_ai_services_account import (
    RESOURCE_TYPE,
    empty_ai_services_account,
)

FIXTURE = load_fixture("empty_ai_services_account")


def _findings(make_context):
    ctx, arm = make_context(
        {
            RESOURCE_TYPE: FIXTURE["accounts"],
            "accounts/ai-used/deployments": FIXTURE["used_deployments"],
            "accounts/ai-project/projects": FIXTURE["project_projects"],
        }
    )
    return {f.resource_id: f for f in empty_ai_services_account(ctx)}, arm


def test_only_an_account_with_no_deployment_and_no_project_is_reported(make_context):
    """A project can use models hosted elsewhere; deleting the account deletes it."""
    findings, _ = _findings(make_context)
    assert set(findings) == {"ai-empty"}


def test_an_account_kind_with_no_deployments_is_not_looked_at(make_context):
    _, arm = _findings(make_context)
    assert not any("accounts/speech/" in path for path in arm.calls)


def test_an_empty_account_costs_nothing_and_says_so(make_context):
    findings, _ = _findings(make_context)
    assert findings["ai-empty"].monthly_cost == 0.0
    assert "48 hours" in findings["ai-empty"].details["note"]
