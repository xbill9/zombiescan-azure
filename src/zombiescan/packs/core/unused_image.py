"""Custom images nothing boots from.

A custom image bills for its stored bytes for as long as it exists. Images
pile up fastest where a build pipeline publishes one per commit and nothing
ever prunes them.

An image is reported when no instance and no instance template references it,
and it is not the newest member of its image family -- ``family`` is how a
template says "always the latest", so the newest image in a family is live
even though nothing names it directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-image"

# An image Google is still willing to boot. DEPRECATED and OBSOLETE images
# still bill, so they are in scope.
_ACTIVE = "READY"


def _newest_per_family(images: list[dict[str, Any]]) -> set[str]:
    """The current image of each family, which a template may resolve to.

    Deleting it would change what ``--image-family`` boots, so it is never
    reported however old it looks.
    """
    newest: dict[str, dict[str, Any]] = {}
    for image in images:
        family = image.get("family")
        if not family:
            continue
        current = newest.get(family)
        if current is None or str(image.get("creationTimestamp", "")) > str(
            current.get("creationTimestamp", "")
        ):
            newest[family] = image
    return {image["name"] for image in newest.values()}


def _storage_region(image: dict[str, Any]) -> str:
    """Where the image's bytes sit, which is what prices it.

    An image is a global resource but its storage is billed regionally, and
    ``storageLocations`` is the only field that says where. A multi-region
    ("us") has no entry in the price table, so it falls through to the default
    region and the finding is marked approximate.
    """
    locations = image.get("storageLocations") or []
    return locations[0] if locations else gcp.GLOBAL


def build_finding(ctx: ScanContext, image: dict[str, Any]) -> Finding:
    name = image["name"]
    # archiveSizeBytes is the compressed size Google bills, not the disk size
    # the image restores to.
    size_gb = helpers.bytes_to_gb(image.get("archiveSizeBytes"))
    price, approximate = ctx.pricing.rate(
        "image.gb_month", region=gcp.region_of(_storage_region(image))
    )
    age = helpers.age_days(image.get("creationTimestamp"))
    deprecated = (image.get("deprecated") or {}).get("state")

    reason = "Custom image referenced by no instance or instance template"
    if deprecated:
        reason += f"; marked {deprecated.lower()}"
    if age is not None:
        reason += f"; created {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="compute-image",
        project=ctx.project,
        location=gcp.GLOBAL,
        reason=reason,
        monthly_cost=size_gb * price,
        remediation=(
            f"gcloud compute images delete {helpers.arg(name)} --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "stored_gb": round(size_gb, 2),
            "disk_size_gb": helpers.gb(image.get("diskSizeGb")),
            "family": image.get("family"),
            "age_days": age,
            "deprecated_state": deprecated,
            "storage_locations": image.get("storageLocations") or [],
            "labels": image.get("labels") or {},
            "usd_per_gb_month": price,
        },
    )


@check(CHECK_NAME, "Custom images nothing boots from", apis="compute")
def unused_image(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("compute")

    # Only this project's own images. The list call would otherwise be empty
    # anyway -- public images live in projects like debian-cloud -- but saying
    # so keeps the intent clear.
    images = [
        image
        for image in gcp.paginate(client, "images", project=ctx.project)
        if image.get("status") == _ACTIVE
    ]
    if not images:
        return

    referenced: set[str] = set()
    for _scope, instance in gcp.aggregated(client, "instances", "instances", project=ctx.project):
        for disk in instance.get("disks") or []:
            source = (disk.get("initializeParams") or {}).get("sourceImage")
            referenced |= helpers.self_link_names([source])
    for _scope, template in gcp.aggregated(
        client, "instanceTemplates", "instanceTemplates", project=ctx.project
    ):
        properties = template.get("properties") or {}
        for disk in properties.get("disks") or []:
            params = disk.get("initializeParams") or {}
            referenced |= helpers.self_link_names([params.get("sourceImage")])
            # A template pinned to a family resolves to whatever is newest,
            # so the family's current image counts as referenced.
            referenced |= helpers.self_link_names([params.get("sourceImageFamily")])

    keep = referenced | _newest_per_family(images)
    for image in images:
        if image["name"] in keep or image.get("family") in keep:
            continue
        yield build_finding(ctx, image)
