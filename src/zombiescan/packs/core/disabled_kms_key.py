"""Cloud KMS key versions that are disabled but still billing.

A disabled key version bills exactly the same as an enabled one. Only
destroying it stops the charge, and Google deliberately makes that a
scheduled, reversible operation rather than an immediate delete -- which is
why disabling a key and moving on is such a common way to keep paying for it.

Cloud KMS rejects ``locations/-``, so this check enumerates the project's KMS
locations and walks them in parallel.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "disabled-kms-key"

DISABLED = "DISABLED"

# HSM-backed key versions cost far more than software ones, so the protection
# level decides the rate.
_LEVELS = {"HSM": "hsm", "SOFTWARE": "software", "EXTERNAL": "software"}


def build_finding(
    ctx: ScanContext, location: str, key: dict[str, Any], version: dict[str, Any]
) -> Finding:
    # A version's resource name carries its whole path, which is what the
    # destroy command needs and what makes the finding identifiable.
    full_name = version["name"]
    parts = full_name.split("/")
    key_ring = parts[parts.index("keyRings") + 1] if "keyRings" in parts else ""
    key_name = parts[parts.index("cryptoKeys") + 1] if "cryptoKeys" in parts else ""
    version_id = gcp.last_segment(full_name)

    level = version.get("protectionLevel", "SOFTWARE")
    price, approximate = ctx.pricing.rate(
        "kms.key_version_month",
        region=gcp.region_of(location),
        variant=_LEVELS.get(level, "software"),
    )
    age = helpers.age_days(version.get("createTime"))

    reason = (
        f"Key version {version_id} of '{key_name}' is disabled, which does not stop the "
        f"charge -- only destroying it does"
    )
    if age is not None:
        reason += f"; created {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{key_ring}/{key_name}/{version_id}",
        resource_type="kms-key-version",
        project=ctx.project,
        location=location,
        reason=reason,
        monthly_cost=price,
        remediation=(
            f"gcloud kms keys versions destroy {helpers.arg(version_id)} --key={key_name} "
            f"--keyring={key_ring} --location={location} --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "key_ring": key_ring,
            "key": key_name,
            "version": version_id,
            "protection_level": level,
            "algorithm": version.get("algorithm"),
            "purpose": key.get("purpose"),
            "age_days": age,
            "note": (
                "destroying a key version is scheduled, not immediate: Google holds it "
                "for 24 hours by default so it can be restored"
            ),
        },
    )


@check(CHECK_NAME, "Disabled KMS key versions still billing", apis="cloudkms")
def disabled_kms_key(ctx: ScanContext) -> Iterator[Finding]:
    def keys_in(client: Any, location: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        found = []
        for ring in gcp.paginate(
            client,
            "projects.locations.keyRings",
            key="keyRings",
            parent=f"{ctx.parent}/locations/{location}",
        ):
            for key in gcp.paginate(
                client,
                "projects.locations.keyRings.cryptoKeys",
                key="cryptoKeys",
                parent=ring["name"],
            ):
                for version in gcp.paginate(
                    client,
                    "projects.locations.keyRings.cryptoKeys.cryptoKeyVersions",
                    key="cryptoKeyVersions",
                    parent=key["name"],
                ):
                    if version.get("state") == DISABLED:
                        found.append((key, version))
        return found

    for location, (key, version) in helpers.across_locations(ctx, "cloudkms", keys_in):
        yield build_finding(ctx, location, key, version)
