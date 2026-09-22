"""The declarative check builder.

What is tested here is the builder's own behaviour: the parts a pack author
would get wrong.
"""

from __future__ import annotations

import pytest

from tests.conftest import SUBSCRIPTION
from zombiescan.building import simple_check
from zombiescan.registry import CHECKS

WIDGETS = "Acme.Widgets/widgets"


def _widget(name="w-1", group="test-rg", location="eastus", **extra):
    return {
        "id": (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group}/providers/{WIDGETS}/{name}"),
        "name": name,
        "location": location,
        "resourceGroup": group,
        **extra,
    }


@pytest.fixture(autouse=True)
def _drop_test_checks():
    """Registering a check is global; these are not real ones."""
    before = set(CHECKS)
    yield
    for name in set(CHECKS) - before:
        del CHECKS[name]


def _widget_check(name="t-check", **overrides):
    kwargs = {
        "resource_type": WIDGETS,
        "kind": "widget",
        "reason": "idle",
        "command": "az widget delete --name {id}",
    }
    kwargs.update(overrides)
    return simple_check(name, "Widgets", **kwargs)


def test_it_lists_and_filters(make_context):
    check = _widget_check(name="t-filter", where=lambda w: w["status"] == "Idle")
    ctx, _ = make_context(
        {WIDGETS: {"value": [_widget("w-1", status="Idle"), _widget("w-2", status="Busy")]}}
    )
    assert [f.resource_id for f in check(ctx)] == ["w-1"]


def test_templates_see_the_item_the_id_the_group_and_the_subscription(make_context):
    check = _widget_check(
        name="t-template",
        reason="{id} in {location} of {group} is {status}",
        command="az widget delete --name {id}",
    )
    ctx, _ = make_context({WIDGETS: {"value": [_widget("w-1", status="Idle")]}})
    finding = next(iter(check(ctx)))
    assert finding.reason == "w-1 in eastus of test-rg is Idle"
    assert finding.remediation == (
        f"az widget delete --name w-1 --resource-group test-rg --subscription {SUBSCRIPTION}"
    )


def test_the_command_builder_adds_the_group_and_subscription(make_context):
    """A check author writes the verb; nothing else should be their problem."""
    check = _widget_check(name="t-command", command="az disk delete --name {id}")
    ctx, _ = make_context({WIDGETS: {"value": [_widget("w-1")]}})
    finding = next(iter(check(ctx)))
    # `az disk delete` prompts, so --yes is added; a command that does not
    # prompt would not get one.
    assert finding.remediation.endswith("--yes")


def test_a_resource_graph_query_is_used_in_place_of_the_list_call(make_context):
    check = _widget_check(name="t-graph", graph="Resources | where type =~ 'acme.widgets/widgets'")
    ctx, arm = make_context(graph={"acme.widgets/widgets": [_widget("w-1"), _widget("w-2")]})
    assert [f.resource_id for f in check(ctx)] == ["w-1", "w-2"]
    assert arm.call_log == [], "the ARM list call was made as well as the query"
    assert len(arm.graph_log) == 1


def test_the_location_and_group_come_off_the_resource(make_context):
    check = _widget_check(name="t-location")
    ctx, _ = make_context(
        {WIDGETS: {"value": [_widget("w-1", group="other-rg", location="westeurope")]}}
    )
    finding = next(iter(check(ctx)))
    assert finding.location == "westeurope"
    assert finding.resource_group == "other-rg"
    assert finding.arm_id.endswith("/other-rg/providers/Acme.Widgets/widgets/w-1")


def test_a_group_missing_from_the_row_is_read_out_of_the_arm_id(make_context):
    """A Resource Graph projection can leave `resourceGroup` out; the id has it."""
    check = _widget_check(name="t-group-from-id")
    row = _widget("w-1", group="derived-rg")
    row.pop("resourceGroup")
    ctx, _ = make_context({WIDGETS: {"value": [row]}})
    assert next(iter(check(ctx))).resource_group == "derived-rg"


def test_a_check_with_no_rate_costs_nothing(make_context):
    """Hygiene findings are free, and that is the right answer rather than a guess."""
    check = _widget_check(name="t-free")
    ctx, _ = make_context({WIDGETS: {"value": [_widget("w-1")]}})
    finding = next(iter(check(ctx)))
    assert finding.monthly_cost == 0.0
    assert finding.approximate_cost is False


def test_quantity_multiplies_the_rate(make_context):
    check = _widget_check(
        name="t-priced",
        rate="snapshot.gb_month",
        quantity=lambda w: float(w["sizeGb"]),
    )
    ctx, _ = make_context({WIDGETS: {"value": [_widget("w-1", sizeGb="40")]}})
    # 40 GiB at the eastus snapshot rate of 0.05
    assert next(iter(check(ctx))).monthly_cost == pytest.approx(2.0)


def test_a_region_the_table_does_not_know_falls_back_and_says_so(make_context):
    check = _widget_check(name="t-fallback", rate="snapshot.gb_month")
    ctx, _ = make_context({WIDGETS: {"value": [_widget("w-1", location="southindia")]}})
    finding = next(iter(check(ctx)))
    assert finding.monthly_cost == pytest.approx(0.05)
    assert finding.approximate_cost is True


def test_a_priced_region_is_not_marked_approximate(make_context):
    check = _widget_check(name="t-exact", rate="snapshot.gb_month")
    ctx, _ = make_context({WIDGETS: {"value": [_widget("w-1", location="westeurope")]}})
    finding = next(iter(check(ctx)))
    assert finding.monthly_cost == pytest.approx(0.055)
    assert finding.approximate_cost is False


def test_the_provider_is_taken_from_the_resource_type(make_context):
    """A pack author names the type once, and the registration pre-check follows."""
    _widget_check(name="t-provider")
    assert CHECKS["t-provider"].providers == ("Acme.Widgets",)


def test_the_check_is_attributed_to_the_module_that_declared_it():
    """explain_finding reads the module docstring; building.py explains the wrong thing."""
    check = _widget_check(name="t-module")
    assert check.__module__ == __name__


def test_registering_the_same_name_twice_is_refused():
    _widget_check(name="t-dupe")
    with pytest.raises(ValueError, match="duplicate check name"):
        _widget_check(name="t-dupe")
