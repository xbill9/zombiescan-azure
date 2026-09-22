"""Managed images nothing boots from.

A managed image is a full copy of a VM's disks, billed as snapshot storage for
as long as it exists. Images are made to be used once -- capture a VM,
deploy from it, forget it -- and nothing in the portal connects an image back
to whether anything still deploys from it.

An image is reported when no VM in the subscription was built from it. That is
a narrower test than "nobody uses it": a VM scale set, a deployment template
in a repository, or a VM in another subscription can all reference an image
this scan cannot see, which is why the cleaner refuses and the finding says
what to check.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-image"

RESOURCE_TYPE = "Microsoft.Compute/images"


def _image_size_gb(image: dict[str, Any]) -> float:
    """Every disk in the image, summed.

    An image of a VM with data disks holds all of them, and the storage
    profile is the only place the sizes appear.
    """
    profile = helpers.properties(image).get("storageProfile") or {}
    disks = [profile.get("osDisk") or {}, *(profile.get("dataDisks") or [])]
    return sum(helpers.gb(disk.get("diskSizeGB")) for disk in disks)


def build_finding(ctx: ScanContext, image: dict[str, Any]) -> Finding:
    name = image["name"]
    arm_id = image.get("id") or ""
    group = image.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(image)
    properties = helpers.properties(image)

    size_gb = _image_size_gb(image)
    price, approximate = ctx.pricing.rate("snapshot.gb_month", region=azure.region_of(location))
    profile = properties.get("storageProfile") or {}

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="managed-image",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"{size_gb:g} GiB managed image that no VM in this subscription was built "
            f"from, billed as snapshot storage for as long as it exists"
        ),
        monthly_cost=size_gb * price,
        remediation=helpers.az(
            f"az image delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate,
        details={
            "size_gb": size_gb,
            "os_type": (profile.get("osDisk") or {}).get("osType"),
            "data_disks": len(profile.get("dataDisks") or []),
            "hyper_v_generation": properties.get("hyperVGeneration"),
            "tags": image.get("tags") or {},
            "usd_per_gb_month": price,
            "note": (
                "a VM scale set, a deployment template or a VM in another subscription "
                "can reference this image without appearing in this scan"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Managed images nothing boots from",
    providers="Microsoft.Compute",
    uncleanable=(
        "an image can be referenced by a VM scale set, an Azure Compute Gallery "
        "version, a deployment template or a VM in another subscription, none of "
        "which this scan can see. Deleting one that is still referenced breaks the "
        "next scale-out rather than failing at delete time, so zombiescan reports "
        "the image and leaves the decision to you"
    ),
)
def unused_image(ctx: ScanContext) -> Iterator[Finding]:
    referenced: set[str] = set()
    for vm in ctx.list("Microsoft.Compute/virtualMachines"):
        profile = helpers.properties(vm).get("storageProfile") or {}
        reference = (profile.get("imageReference") or {}).get("id")
        if reference:
            referenced.add(str(reference).lower())

    for image in ctx.list(RESOURCE_TYPE):
        if str(image.get("id") or "").lower() in referenced:
            continue
        yield build_finding(ctx, image)
