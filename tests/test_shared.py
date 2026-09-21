"""The helpers more than one check depends on."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import FakeClient
from zombiescan import gcp, helpers


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("us-central1-a", "us-central1"),
        ("us-central1", "us-central1"),
        ("europe-west1-b", "europe-west1"),
        ("northamerica-northeast2-c", "northamerica-northeast2"),
        ("global", "global"),
        ("", "global"),
    ],
)
def test_a_zone_prices_as_its_region(location, expected):
    """Pricing is regional, so every lookup keys on the region a zone is in."""
    assert gcp.region_of(location) == expected


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("zones/us-central1-a", "us-central1-a"),
        ("regions/europe-west4", "europe-west4"),
        ("global", "global"),
    ],
)
def test_location_comes_out_of_the_aggregation_scope(scope, expected):
    assert gcp.location_from_scope(scope) == expected


def test_last_segment_reads_a_name_off_a_resource_url():
    url = "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/disks/data-1"
    assert gcp.last_segment(url) == "data-1"
    assert gcp.last_segment(None) == ""
    assert gcp.last_segment("") == ""


@pytest.mark.parametrize(
    ("location", "flag"),
    [
        ("us-central1-a", "--zone=us-central1-a"),
        ("us-central1", "--region=us-central1"),
        ("global", ""),
    ],
)
def test_location_flag_matches_how_gcloud_addresses_the_resource(location, flag):
    """A wrong flag makes a generated command prompt, which a script cannot answer."""
    assert helpers.location_flag(location) == flag


def test_gcloud_always_names_the_project_and_never_prompts():
    command = helpers.gcloud("gcloud compute disks delete d1", "proj-1", "us-central1-a")
    assert command == (
        "gcloud compute disks delete d1 --zone=us-central1-a --project=proj-1 --quiet"
    )


def test_gcloud_omits_the_location_flag_for_a_global_resource():
    assert helpers.gcloud("gcloud compute images delete i1", "proj-1") == (
        "gcloud compute images delete i1 --project=proj-1 --quiet"
    )


def test_age_days_reads_both_timestamp_spellings():
    """Google returns some timestamps with an offset and some ending in Z."""
    recent = (dt.datetime.now(dt.UTC) - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert helpers.age_days(recent) == 10
    offset = (dt.datetime.now(dt.UTC) - dt.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    assert helpers.age_days(offset) == 5


def test_age_days_is_none_when_there_is_no_timestamp():
    assert helpers.age_days(None) is None
    assert helpers.age_days("not a date") is None


def test_sizes_parse_from_the_strings_google_returns():
    assert helpers.gb("100") == 100.0
    assert helpers.gb(None) == 0.0
    assert helpers.bytes_to_gb(str(5 * 1024**3)) == pytest.approx(5.0)


def test_disk_variant_is_the_last_segment_of_the_type_url():
    disk = {"type": "https://compute.googleapis.com/v1/projects/p/zones/z/diskTypes/pd-ssd"}
    assert helpers.disk_variant(disk) == "pd-ssd"
    # A disk with no type at all is priced as the oldest default rather than
    # crashing the check that found it.
    assert helpers.disk_variant({}) == "pd-standard"


def test_instances_are_counted_per_network(make_context):
    ctx, _ = make_context(
        {
            "instances.aggregatedList": {
                "items": {
                    "zones/us-central1-a": {
                        "instances": [
                            {"name": "a", "networkInterfaces": [{"network": ".../networks/prod"}]},
                            {"name": "b", "networkInterfaces": [{"network": ".../networks/prod"}]},
                        ]
                    },
                    "zones/us-east1-b": {
                        "instances": [
                            {"name": "c", "networkInterfaces": [{"network": ".../networks/dev"}]}
                        ]
                    },
                    "zones/us-west1-a": {"warning": {"code": "NO_RESULTS_ON_PAGE"}},
                }
            }
        }
    )
    assert helpers.instances_by_network(ctx) == {"prod": 2, "dev": 1}


def test_aggregated_skips_scopes_that_hold_only_a_warning():
    """Compute reports an empty zone as a warning, not as an empty list."""
    client = FakeClient(
        {
            "disks.aggregatedList": {
                "items": {
                    "zones/us-central1-a": {"disks": [{"name": "d1"}]},
                    "zones/us-east1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}},
                }
            }
        }
    )
    assert list(gcp.aggregated(client, "disks", "disks", project="p")) == [
        ("zones/us-central1-a", {"name": "d1"})
    ]


def test_call_resolves_a_dotted_operation_path():
    """A Step names its call as data, so the runner has to walk the path."""
    client = FakeClient({"projects.secrets.delete": {"done": True}})
    assert gcp.call(client, "projects.secrets.delete", name="projects/p/secrets/s") == {
        "done": True
    }
    assert client.calls["projects.secrets.delete"] == {"name": "projects/p/secrets/s"}
