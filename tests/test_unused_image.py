"""Managed images nothing boots from."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.unused_image import CHECK_NAME, unused_image
from zombiescan.registry import CHECKS

FIXTURE = load_fixture("unused_image")


def _findings(make_context):
    ctx, arm = make_context(
        {
            "Microsoft.Compute/images": FIXTURE["images"],
            "Microsoft.Compute/virtualMachines": FIXTURE["vms"],
        }
    )
    return list(unused_image(ctx)), arm


def test_an_image_a_vm_was_built_from_is_not_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"golden-2024"}


def test_every_disk_in_the_image_is_counted(make_context):
    """An image of a VM with data disks holds all of them."""
    findings, _ = _findings(make_context)
    image = findings[0]
    assert image.details["size_gb"] == 96
    assert image.details["data_disks"] == 1
    assert image.monthly_cost == 96 * 0.05


def test_the_check_refuses_to_clean_and_says_why(make_context):
    """A scale set or another subscription can reference an image unseen."""
    spec = CHECKS[CHECK_NAME]
    assert spec.uncleanable
    assert "scale set" in spec.uncleanable


def test_the_unseen_references_are_named_in_the_finding(make_context):
    findings, _ = _findings(make_context)
    assert "another subscription" in findings[0].details["note"]
