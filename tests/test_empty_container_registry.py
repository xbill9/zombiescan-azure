"""Container registries holding no images."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.azure import ArmError
from zombiescan.packs.core.empty_container_registry import empty_container_registry

FIXTURE = load_fixture("empty_container_registry")


def _responses(usages):
    return {"Microsoft.ContainerRegistry/registries": FIXTURE["registries"], "/listUsages": usages}


def _usage_for(path: str):
    return FIXTURE["usages_empty"] if "oldbuilds" in path else FIXTURE["usages_full"]


def _findings(make_context):
    ctx, arm = make_context(_responses(lambda path: _usage_for(path)))
    return list(empty_container_registry(ctx)), arm


def test_a_registry_holding_images_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"oldbuilds"}


def test_an_empty_registry_costs_its_full_tier_fee(make_context):
    """Azure bills a registry by tier per day. Its contents do not enter into it."""
    findings, _ = _findings(make_context)
    registry = findings[0]
    assert registry.details["sku"] == "Premium"
    assert round(registry.monthly_cost, 2) == round(1.6666 * (730 / 24), 2)
    assert "regardless of what is in it" in registry.reason


def test_a_registry_that_refuses_the_usage_call_is_not_assumed_empty(make_context):
    """A firewalled registry answers with an error, which is not evidence."""
    refused = ArmError(403, "Forbidden", "private endpoint only")
    ctx, _ = make_context(_responses(lambda path: refused))
    assert list(empty_container_registry(ctx)) == []


def test_the_figure_is_the_tier_fee_and_says_so(make_context):
    findings, _ = _findings(make_context)
    assert "tier fee only" in findings[0].details["note"]
