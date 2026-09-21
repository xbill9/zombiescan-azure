"""Artifact Registry repositories nothing has pushed to in a long time.

Container images are the easiest thing in a cloud account to accumulate: a CI
pipeline pushes one per commit, storage bills per GB-month, and nobody prunes.
A repository with no push in 90 days is one whose pipeline has moved on.

The cost is an explicit **upper bound**. Artifact Registry bills unique layers
once, so two images sharing a base layer are billed for that layer once while
``sizeBytes`` counts it in both. The finding says so rather than overstating
the saving silently.

Artifact Registry rejects ``locations/-``, so this check enumerates the
project's repository locations and walks them in parallel.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "stale-artifact-repository"

STALE_AFTER_DAYS = 90


def build_finding(ctx: ScanContext, location: str, repository: dict[str, Any]) -> Finding:
    name = gcp.last_segment(repository.get("name"))
    size_gb = helpers.bytes_to_gb(repository.get("sizeBytes"))
    price, approximate = ctx.pricing.rate("artifact.gb_month")
    idle = helpers.age_days(repository.get("updateTime"))

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="artifact-repository",
        project=ctx.project,
        location=location,
        reason=(
            f"{repository.get('format', 'Artifact')} repository holding {size_gb:.1f} GB "
            f"with nothing pushed to it in {idle} days"
        ),
        monthly_cost=size_gb * price,
        # Marked approximate because the figure is an upper bound, not because
        # the rate was guessed: shared layers are billed once and counted twice.
        approximate_cost=True,
        remediation=(
            f"gcloud artifacts repositories delete {helpers.arg(name)} --location={location} "
            f"--project={ctx.project} --quiet"
        ),
        details={
            "stored_gb": round(size_gb, 2),
            "format": repository.get("format"),
            "mode": repository.get("mode"),
            "idle_days": idle,
            "labels": repository.get("labels") or {},
            "usd_per_gb_month": price,
            "rate_is_exact": not approximate,
            "note": (
                "upper bound: Artifact Registry bills each unique layer once, and images "
                "sharing a base layer are counted once per image here"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Artifact Registry repositories with no recent pushes",
    apis="artifactregistry",
)
def stale_artifact_repository(ctx: ScanContext) -> Iterator[Finding]:
    def repositories_in(client: Any, location: str) -> list[dict[str, Any]]:
        return list(
            gcp.paginate(
                client,
                "projects.locations.repositories",
                key="repositories",
                parent=f"{ctx.parent}/locations/{location}",
            )
        )

    for location, repository in helpers.across_locations(ctx, "artifactregistry", repositories_in):
        idle = helpers.age_days(repository.get("updateTime"))
        if idle is None or idle < STALE_AFTER_DAYS:
            continue
        # An empty repository costs nothing; reporting it is noise. Google
        # returns the size as a string, so "0" is truthy and the value has to
        # be read as a number rather than tested for presence.
        if helpers.gb(repository.get("sizeBytes")) <= 0:
            continue
        yield build_finding(ctx, location, repository)
