from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_image import unused_image

FIXTURE = load_fixture("unused_image")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "images.list": FIXTURE["images"],
            "instances.aggregatedList": FIXTURE["instances"],
            "instanceTemplates.aggregatedList": FIXTURE["templates"],
        }
    )
    return {f.resource_id: f for f in unused_image(ctx)}


def test_an_image_nothing_references_is_flagged(findings):
    assert "deprecated-leftover" in findings


def test_an_image_an_instance_booted_from_is_kept(findings):
    assert "base-referenced-by-instance" not in findings


def test_an_image_an_instance_template_names_is_kept(findings):
    assert "base-referenced-by-template" not in findings


def test_the_newest_image_of_a_family_is_never_reported(findings):
    """A template pinned to --image-family resolves to it, so deleting it
    changes what the next instance boots."""
    assert "app-v2" not in findings


def test_an_older_image_of_a_family_is_still_reported(findings):
    assert "app-v1" in findings


def test_an_image_that_is_not_ready_is_skipped(findings):
    """A PENDING image is still being built, not abandoned."""
    assert "still-building" not in findings


def test_a_deprecated_image_still_bills_and_says_so(findings):
    finding = findings["deprecated-leftover"]
    assert "deprecated" in finding.reason
    assert finding.details["deprecated_state"] == "DEPRECATED"


def test_cost_is_the_compressed_archive_size(findings):
    """archiveSizeBytes is what Google bills; diskSizeGb is what it restores to."""
    # 10 GiB at the europe-west1 image rate of 0.05
    assert findings["deprecated-leftover"].monthly_cost == pytest.approx(0.5)
    assert findings["deprecated-leftover"].details["disk_size_gb"] == 50.0


def test_an_image_is_global_but_priced_where_it_is_stored(findings):
    finding = findings["deprecated-leftover"]
    assert finding.location == "global"
    assert finding.details["storage_locations"] == ["europe-west1"]
    assert finding.approximate_cost is False


def test_the_delete_command_takes_no_location_flag(findings):
    assert findings["app-v1"].remediation == (
        "gcloud compute images delete app-v1 --project=test-project --quiet"
    )
