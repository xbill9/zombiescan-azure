"""The declarative check builder.

What is tested here is the builder's own behaviour: the parts a pack author
would get wrong.
"""

from __future__ import annotations

import pytest

from zombiescan.building import simple_check
from zombiescan.registry import CHECKS


@pytest.fixture(autouse=True)
def _drop_test_checks():
    """Registering a check is global; these are not real ones."""
    before = set(CHECKS)
    yield
    for name in set(CHECKS) - before:
        del CHECKS[name]


def _widget_check(name="t-check", **overrides):
    kwargs = {
        "api": "compute",
        "path": "widgets",
        "resource_type": "widget",
        "id_key": "name",
        "reason": "idle",
        "remediation": "gcloud widgets delete {id}",
    }
    kwargs.update(overrides)
    return simple_check(name, "Widgets", **kwargs)


def test_it_lists_and_filters(make_context):
    check = simple_check(
        "t-filter",
        "Widgets",
        api="compute",
        path="widgets",
        resource_type="widget",
        id_key="name",
        where=lambda w: w["status"] == "IDLE",
        reason="idle",
        remediation="gcloud widgets delete {id}",
    )
    ctx, _ = make_context(
        {
            "widgets.list": {
                "items": [{"name": "w-1", "status": "IDLE"}, {"name": "w-2", "status": "BUSY"}]
            }
        }
    )
    assert [f.resource_id for f in check(ctx)] == ["w-1"]


def test_templates_see_the_item_the_id_the_location_and_the_project(make_context):
    check = simple_check(
        "t-template",
        "Widgets",
        api="compute",
        path="widgets",
        resource_type="widget",
        id_key="name",
        location="zone",
        reason="{id} in {location} of {project} is {status}",
        remediation="gcloud widgets delete {id} --zone={location} --project={project}",
    )
    ctx, _ = make_context(
        {"widgets.list": {"items": [{"name": "w-1", "status": "IDLE", "zone": "us-central1-a"}]}}
    )
    finding = next(iter(check(ctx)))
    assert finding.reason == "w-1 in us-central1-a of test-project is IDLE"
    assert finding.remediation == (
        "gcloud widgets delete w-1 --zone=us-central1-a --project=test-project"
    )


def test_aggregated_mode_takes_the_location_from_the_scope(make_context):
    check = simple_check(
        "t-aggregated",
        "Widgets",
        api="compute",
        path="widgets",
        aggregated_key="widgets",
        resource_type="widget",
        id_key="name",
        reason="idle",
        remediation="gcloud widgets delete {id}",
    )
    ctx, client = make_context(
        {
            "widgets.aggregatedList": {
                "items": {
                    "zones/us-central1-a": {"widgets": [{"name": "w-1"}]},
                    "regions/europe-west1": {"widgets": [{"name": "w-2"}]},
                }
            }
        }
    )
    findings = {f.resource_id: f for f in check(ctx)}
    assert findings["w-1"].location == "us-central1-a"
    assert findings["w-2"].location == "europe-west1"
    assert "widgets.aggregatedList" in client.calls


def test_a_check_with_no_rate_costs_nothing(make_context):
    """Hygiene findings are free, and that is the right answer rather than a guess."""
    check = _widget_check(name="t-free")
    ctx, _ = make_context({"widgets.list": {"items": [{"name": "w-1"}]}})
    finding = next(iter(check(ctx)))
    assert finding.monthly_cost == 0.0
    assert finding.approximate_cost is False


def test_quantity_multiplies_the_rate(make_context):
    check = simple_check(
        "t-priced",
        "Widgets",
        api="compute",
        path="widgets",
        resource_type="widget",
        id_key="name",
        location="zone",
        rate="snapshot.gb_month",
        quantity=lambda w: float(w["sizeGb"]),
        reason="idle",
        remediation="gcloud widgets delete {id}",
    )
    ctx, _ = make_context(
        {"widgets.list": {"items": [{"name": "w-1", "sizeGb": "40", "zone": "us-central1-a"}]}}
    )
    # 40 GB at the us-central1 snapshot rate of 0.05
    assert next(iter(check(ctx))).monthly_cost == pytest.approx(2.0)


def test_a_zone_prices_as_its_region(make_context):
    check = simple_check(
        "t-zone-price",
        "Widgets",
        api="compute",
        path="widgets",
        resource_type="widget",
        id_key="name",
        location="zone",
        rate="snapshot.gb_month",
        reason="idle",
        remediation="gcloud widgets delete {id}",
    )
    ctx, _ = make_context({"widgets.list": {"items": [{"name": "w-1", "zone": "europe-west1-d"}]}})
    finding = next(iter(check(ctx)))
    assert finding.monthly_cost == pytest.approx(0.055)
    assert finding.approximate_cost is False


def test_params_may_be_computed_from_the_context(make_context):
    check = simple_check(
        "t-params",
        "Widgets",
        api="secretmanager",
        path="projects.secrets",
        result_key="secrets",
        resource_type="widget",
        id_key="name",
        reason="idle",
        remediation="gcloud widgets delete {id}",
        params=lambda ctx: {"parent": ctx.parent},
    )
    ctx, client = make_context({"projects.secrets.list": {"secrets": [{"name": "s-1"}]}})
    list(check(ctx))
    assert client.calls["projects.secrets.list"] == {"parent": "projects/test-project"}


def test_the_check_is_attributed_to_the_module_that_declared_it():
    """explain_finding reads the module docstring; building.py explains the wrong thing."""
    check = _widget_check(name="t-module")
    assert check.__module__ == __name__
    assert CHECKS["t-module"].apis == ("compute",)


def test_registering_the_same_name_twice_is_refused():
    _widget_check(name="t-dupe")
    with pytest.raises(ValueError, match="duplicate check name"):
        _widget_check(name="t-dupe")
