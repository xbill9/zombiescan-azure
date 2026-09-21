"""Helpers shared by more than one check."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import shlex
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import googleapiclient.errors

from zombiescan import gcp
from zombiescan.models import ScanContext


def age_days(timestamp: str | None) -> int | None:
    """Whole days since an RFC 3339 timestamp, or None if it is absent.

    Google returns timestamps as strings rather than as datetimes, and some
    carry a numeric offset while others end in ``Z``; both parse once ``Z`` is
    spelled the way ``fromisoformat`` expects.
    """
    if not timestamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - parsed).days


def gb(value: Any) -> float:
    """A size Google reports as a string of bytes or GB, as a float of GB.

    Disk sizes come back as ``"100"`` GB, Filestore capacity as ``"1024"`` GB,
    and bucket sizes from monitoring as raw bytes. Each call site says which it
    has; this only handles the string-to-number half that all of them share.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def bytes_to_gb(value: Any) -> float:
    """Bytes as GB, using the 2^30 definition Google bills storage by."""
    return gb(value) / (1024**3)


def instances_by_network(ctx: ScanContext) -> dict[str, int]:
    """How many instances sit in each VPC network, keyed by network name.

    Several checks ask the same question -- is anything actually running in
    here? -- of a network, a subnet or a Cloud NAT. One aggregated call answers
    it for all of them.
    """
    counts: dict[str, int] = {}
    for _scope, instance in gcp.aggregated(
        ctx.client("compute"), "instances", "instances", project=ctx.project
    ):
        for interface in instance.get("networkInterfaces") or []:
            network = gcp.last_segment(interface.get("network"))
            if network:
                counts[network] = counts.get(network, 0) + 1
    return counts


def disk_variant(disk: dict[str, Any]) -> str:
    """The rate variant for a disk, from its ``type`` URL.

    A disk's type is a full URL ending in ``pd-balanced``, ``pd-ssd``,
    ``hyperdisk-balanced`` and so on, which is exactly how the price table
    keys its per-GB rates.
    """
    return gcp.last_segment(disk.get("type")) or "pd-standard"


def self_link_names(links: Iterable[Any]) -> set[str]:
    """The final segment of every URL in ``links``, ignoring empties.

    Google expresses "what is using this" as a list of full resource URLs, and
    every check that cross-references two lists wants them as bare names.
    """
    return {name for name in (gcp.last_segment(link) for link in links) if name}


def location_flag(location: str) -> str:
    """The gcloud flag naming where a resource lives.

    Google splits resources three ways and the delete command differs for
    each: a zonal disk takes ``--zone``, a regional one ``--region``, and a
    snapshot or image takes neither. Getting this wrong produces a command
    that prompts for a location, which is exactly what a generated cleanup
    script must never do.
    """
    if not location or location == gcp.GLOBAL:
        return ""
    if gcp.region_of(location) == location:
        return f"--region={location}"
    return f"--zone={location}"


def gcloud(command: str, project: str, location: str = "") -> str:
    """A complete, non-interactive gcloud command.

    Every generated command carries its project and its location explicitly,
    so pasting one into a shell configured for a different project deletes
    nothing by surprise, and ``--quiet`` keeps it from blocking on a prompt.
    """
    parts = [command]
    flag = location_flag(location)
    if flag:
        parts.append(flag)
    parts.append(f"--project={project}")
    parts.append("--quiet")
    return " ".join(parts)


def locations_of(ctx: ScanContext, api: str) -> list[str]:
    """Every location one API serves for this project.

    Cloud KMS and Artifact Registry reject ``locations/-``: their list calls
    want a real location, so a check against either has to enumerate first.
    Both publish the list through the same ``projects.locations.list`` method,
    which is why this is one helper and not two.
    """
    return [
        location["locationId"]
        for location in gcp.paginate(
            ctx.client(api),
            "projects.locations",
            key="locations",
            name=ctx.parent,
        )
        if location.get("locationId")
    ]


def across_locations(
    ctx: ScanContext,
    api: str,
    fetch: Callable[[Any, str], list[Any]],
    workers: int = 12,
) -> Iterator[tuple[str, Any]]:
    """Run ``fetch`` against every location of ``api``, yielding ``(location, item)``.

    Seventy serial round trips to Cloud KMS would dominate the runtime of a
    whole scan, so the locations are walked in parallel. A location that
    refuses the call contributes nothing rather than failing the check: an
    API enabled in one location and not another is ordinary.

    **``fetch`` is handed its client rather than capturing one.** A discovery
    client owns an ``httplib2`` connection that is not safe to use from two
    threads, and a client built in the calling thread and closed over by the
    callback is shared by every worker here -- which segfaults the interpreter
    partway through a scan instead of raising. Passing the client in means the
    one each worker uses is the one ``Clients`` built for that thread, and the
    mistake cannot be written.
    """
    locations = locations_of(ctx, api)

    def run(location: str) -> list[Any]:
        return fetch(ctx.client(api), location)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, location): location for location in locations}
        for future in concurrent.futures.as_completed(futures):
            location = futures[future]
            try:
                items = future.result()
            except googleapiclient.errors.HttpError:
                continue
            for item in items:
                yield location, item


def arg(value: Any) -> str:
    """One shell argument, quoted if it needs it.

    Google's own naming rules keep the characters that matter out of most
    resource ids today. Relying on a remote service's input validation for
    local shell safety is the assumption that ages badly -- a name with a
    quote or a semicolon in it must stay one argument to the generated
    command, not become a second command.

    ``shlex.quote`` leaves an ordinary name untouched, so this costs nothing
    in the normal case and the generated plan stays readable.
    """
    return shlex.quote(str(value))
