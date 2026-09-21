from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.idle_filestore import idle_filestore

FIXTURE = load_fixture("idle_filestore")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        by_api={
            "file": {"projects.locations.instances.list": FIXTURE["instances"]},
            "compute": {"instances.aggregatedList": FIXTURE["instances_compute"]},
        }
    )
    return {f.resource_id: f for f in idle_filestore(ctx)}


def test_a_share_in_a_network_with_no_compute_is_flagged(findings):
    assert "abandoned-share" in findings


def test_a_share_in_a_live_network_is_left_alone(findings):
    assert "prod-share" not in findings


def test_the_location_comes_out_of_the_resource_path(findings):
    assert findings["abandoned-share"].location == "us-central1-a"


def test_cost_is_capacity_at_the_tier_rate(findings):
    # 1024 GB zonal in us-central1 at 0.25
    assert findings["abandoned-share"].monthly_cost == pytest.approx(1024 * 0.25)


def test_a_zonal_share_prices_at_its_region(findings):
    assert findings["abandoned-share"].approximate_cost is False


def test_the_note_warns_that_the_file_data_goes_with_it(findings):
    assert "deletes the file data" in findings["abandoned-share"].details["note"]


def test_one_wildcard_call_covers_every_location(make_context):
    """Filestore accepts locations/-, so there is no per-location loop."""
    ctx, clients = make_context(
        by_api={
            "file": {"projects.locations.instances.list": FIXTURE["instances"]},
            "compute": {"instances.aggregatedList": FIXTURE["instances_compute"]},
        }
    )
    list(idle_filestore(ctx))
    assert clients["file"].calls["projects.locations.instances.list"] == {
        "parent": "projects/test-project/locations/-"
    }


def test_the_check_refuses_to_clean_and_says_why():
    from zombiescan.registry import CHECKS

    reason = CHECKS["idle-filestore"].uncleanable
    assert reason and "backup" in reason
