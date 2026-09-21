"""How core's findings are removed.

A cleaner plans; it never executes. Each one yields the mutating calls that
would resolve a finding, in the order they must happen, and ``clean.py``
decides whether to send them.

Three rules run through all of these:

* **Back up before destroying, where the API allows it.** A disk is
  snapshotted before it is deleted and a Cloud SQL instance gets a backup run,
  and the backup is always the first step -- a failed step aborts the rest of
  that finding, so a failed snapshot can never be followed by the delete that
  assumed it.
* **``irreversible=True`` means no recovery window at all.** A deleted
  snapshot is gone; a destroyed KMS key version is held for 24 hours and can
  be restored, so it is not marked.
* **Refuse rather than guess.** Where the safe action depends on something the
  API does not say, the check carries an ``uncleanable`` reason instead of a
  cleaner here.
"""

from __future__ import annotations

from collections.abc import Iterator

from zombiescan import gcp
from zombiescan.cleaners import Step, backup_name, cleaner
from zombiescan.models import Finding, ScanContext

# --------------------------------------------------------------------------
# Compute Engine
# --------------------------------------------------------------------------


def _is_regional(location: str) -> bool:
    return gcp.region_of(location) == location


@cleaner("unattached-disk")
def clean_unattached_disk(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Snapshot the disk, then delete it.

    The snapshot is what makes the delete safe, so it goes first and the
    delete only runs if it succeeded. A regional disk lives in a different
    collection from a zonal one and has to be addressed through it.
    """
    name = finding.resource_id
    location = finding.location
    snapshot = backup_name(name)

    if _is_regional(location):
        yield Step(
            description=f"snapshot regional disk {name} as {snapshot}",
            api="compute",
            operation="regionDisks.createSnapshot",
            params={
                "project": finding.project,
                "region": location,
                "disk": name,
                "body": {"name": snapshot},
            },
        )
        yield Step(
            description=f"delete regional disk {name}",
            api="compute",
            operation="regionDisks.delete",
            params={"project": finding.project, "region": location, "disk": name},
        )
        return

    yield Step(
        description=f"snapshot disk {name} as {snapshot}",
        api="compute",
        operation="disks.createSnapshot",
        params={
            "project": finding.project,
            "zone": location,
            "disk": name,
            "body": {"name": snapshot},
        },
    )
    yield Step(
        description=f"delete disk {name}",
        api="compute",
        operation="disks.delete",
        params={"project": finding.project, "zone": location, "disk": name},
    )


@cleaner("unused-static-ip")
def clean_unused_static_ip(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Release the address.

    Irreversible in the way that matters: the address is returned to Google's
    pool and cannot be reclaimed, so anything with that IP written into a DNS
    record or an allowlist stops working and cannot be put back.
    """
    name = finding.resource_id
    if finding.location == gcp.GLOBAL:
        yield Step(
            description=f"release global static IP {name}",
            api="compute",
            operation="globalAddresses.delete",
            params={"project": finding.project, "address": name},
            irreversible=True,
        )
        return
    yield Step(
        description=f"release static IP {name} in {finding.location}",
        api="compute",
        operation="addresses.delete",
        params={"project": finding.project, "region": finding.location, "address": name},
        irreversible=True,
    )


@cleaner("orphaned-snapshot")
def clean_orphaned_snapshot(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the snapshot. There is nothing to back a snapshot up with."""
    yield Step(
        description=f"delete snapshot {finding.resource_id}",
        api="compute",
        operation="snapshots.delete",
        params={"project": finding.project, "snapshot": finding.resource_id},
        irreversible=True,
    )


@cleaner("unused-image")
def clean_unused_image(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    yield Step(
        description=f"delete custom image {finding.resource_id}",
        api="compute",
        operation="images.delete",
        params={"project": finding.project, "image": finding.resource_id},
        irreversible=True,
    )


@cleaner("stopped-instance")
def clean_stopped_instance(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Detach the disks from the instance's lifecycle, then delete it.

    Deleting an instance also deletes every attached disk whose ``autoDelete``
    is set, which for a boot disk is the default. Clearing that flag first
    turns an irreversible delete into a recoverable one: the disks survive,
    and the unattached-disk check reports them next run with a snapshot-first
    plan of their own.

    The flag is read during planning and cleared as its own step, so the dry
    run shows exactly which disks are about to be spared.
    """
    name = finding.resource_id
    zone = finding.location
    instance = gcp.call(
        ctx.client("compute"),
        "instances.get",
        project=finding.project,
        zone=zone,
        instance=name,
    )

    for disk in instance.get("disks") or []:
        if not disk.get("autoDelete"):
            continue
        device = disk.get("deviceName")
        if not device:
            continue
        yield Step(
            description=f"keep disk {gcp.last_segment(disk.get('source')) or device} after delete",
            api="compute",
            operation="instances.setDiskAutoDelete",
            params={
                "project": finding.project,
                "zone": zone,
                "instance": name,
                "deviceName": device,
                "autoDelete": False,
            },
        )

    yield Step(
        description=f"delete stopped instance {name}, keeping its disks",
        api="compute",
        operation="instances.delete",
        params={"project": finding.project, "zone": zone, "instance": name},
    )


@cleaner("idle-cloud-nat")
def clean_idle_cloud_nat(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Remove one NAT configuration from its Cloud Router.

    There is no delete call for a NAT: it is a field on the router, so the
    removal is a patch carrying the NATs that should remain. The current list
    is read during planning, which is what makes the dry run show the exact
    body that would be sent.
    """
    router_name, _, nat_name = finding.resource_id.partition("/")
    router = gcp.call(
        ctx.client("compute"),
        "routers.get",
        project=finding.project,
        region=finding.location,
        router=router_name,
    )
    remaining = [nat for nat in (router.get("nats") or []) if nat.get("name") != nat_name]
    if len(remaining) == len(router.get("nats") or []):
        # The NAT is already gone. Yielding no steps reports the finding as
        # having nothing to do rather than sending a patch that changes
        # nothing.
        return

    yield Step(
        description=f"remove NAT {nat_name} from router {router_name}",
        api="compute",
        operation="routers.patch",
        params={
            "project": finding.project,
            "region": finding.location,
            "router": router_name,
            "body": {"nats": remaining},
        },
    )


@cleaner("idle-forwarding-rule")
def clean_idle_forwarding_rule(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    name = finding.resource_id
    if finding.location == gcp.GLOBAL:
        yield Step(
            description=f"delete global forwarding rule {name}",
            api="compute",
            operation="globalForwardingRules.delete",
            params={"project": finding.project, "forwardingRule": name},
        )
        return
    yield Step(
        description=f"delete forwarding rule {name} in {finding.location}",
        api="compute",
        operation="forwardingRules.delete",
        params={"project": finding.project, "region": finding.location, "forwardingRule": name},
    )


@cleaner("unused-firewall-rule")
def clean_unused_firewall_rule(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    yield Step(
        description=f"delete firewall rule {finding.resource_id}",
        api="compute",
        operation="firewalls.delete",
        params={"project": finding.project, "firewall": finding.resource_id},
    )


@cleaner("unused-subnet")
def clean_unused_subnet(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    yield Step(
        description=f"delete subnet {finding.resource_id} in {finding.location}",
        api="compute",
        operation="subnetworks.delete",
        params={
            "project": finding.project,
            "region": finding.location,
            "subnetwork": finding.resource_id,
        },
    )


# --------------------------------------------------------------------------
# Managed services
# --------------------------------------------------------------------------


@cleaner("stopped-sql-instance")
def clean_stopped_sql_instance(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Take a backup, then delete the instance.

    An on-demand backup outlives the instance it came from, so it is the one
    thing that makes deleting a database recoverable. It runs first and the
    delete is abandoned if it fails.
    """
    name = finding.resource_id
    yield Step(
        description=f"back up Cloud SQL instance {name} before deleting it",
        api="sqladmin",
        operation="backupRuns.insert",
        params={
            "project": finding.project,
            "instance": name,
            "body": {"description": f"zombiescan pre-delete backup of {name}"},
        },
    )
    yield Step(
        description=f"delete Cloud SQL instance {name}",
        api="sqladmin",
        operation="instances.delete",
        params={"project": finding.project, "instance": name},
    )


@cleaner("unused-dns-zone")
def clean_unused_dns_zone(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the zone.

    The SOA and NS records go with it and need no separate step: those are the
    only two records the check allows a reported zone to have, and Cloud DNS
    removes them with the zone.
    """
    yield Step(
        description=f"delete managed zone {finding.resource_id}",
        api="dns",
        operation="managedZones.delete",
        params={"project": finding.project, "managedZone": finding.resource_id},
        irreversible=True,
    )


@cleaner("stale-secret")
def clean_stale_secret(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the secret and every version in it.

    Secret Manager has no recycle bin: the secret material is unrecoverable
    the moment this returns, and anything still reading it fails immediately.
    """
    yield Step(
        description=f"delete secret {finding.resource_id} and all its versions",
        api="secretmanager",
        operation="projects.secrets.delete",
        params={"name": f"projects/{finding.project}/secrets/{finding.resource_id}"},
        irreversible=True,
    )


@cleaner("disabled-kms-key")
def clean_disabled_kms_key(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Schedule the key version for destruction.

    Not marked irreversible: Google holds a destroyed version for 24 hours by
    default and ``gcloud kms keys versions restore`` brings it back within
    that window. That is a real recovery path, which is the test this flag
    applies.
    """
    key_ring, key, version = finding.resource_id.split("/")
    name = (
        f"projects/{finding.project}/locations/{finding.location}/keyRings/{key_ring}"
        f"/cryptoKeys/{key}/cryptoKeyVersions/{version}"
    )
    yield Step(
        description=f"schedule destruction of key version {version} of {key}",
        api="cloudkms",
        operation="projects.locations.keyRings.cryptoKeys.cryptoKeyVersions.destroy",
        params={"name": name, "body": {}},
    )


@cleaner("stale-artifact-repository")
def clean_stale_artifact_repository(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    yield Step(
        description=f"delete Artifact Registry repository {finding.resource_id} and its images",
        api="artifactregistry",
        operation="projects.locations.repositories.delete",
        params={
            "name": (
                f"projects/{finding.project}/locations/{finding.location}"
                f"/repositories/{finding.resource_id}"
            )
        },
        irreversible=True,
    )


@cleaner("unused-uptime-check")
def clean_unused_uptime_check(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    yield Step(
        description=f"delete uptime check {finding.resource_id}",
        api="monitoring",
        operation="projects.uptimeCheckConfigs.delete",
        params={"name": f"projects/{finding.project}/uptimeCheckConfigs/{finding.resource_id}"},
    )
