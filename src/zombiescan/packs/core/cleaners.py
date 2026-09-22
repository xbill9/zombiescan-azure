"""How core's findings are removed.

Every cleaner here yields ARM calls and executes none of them. The runner in
``zombiescan.clean`` decides whether the plan is sent; ``--apply`` changes
that and nothing about which steps get planned.

**Azure has more recovery windows than most clouds, and the ``irreversible``
flag reflects the real ones rather than a general sense of danger.** A deleted
Key Vault key is recoverable for the vault's soft-delete period. A deleted SQL
database is restorable from its point-in-time backups. A deleted managed disk
is not -- which is why the disk cleaner takes a snapshot first -- and a
released public IP address is gone for good, which is why that one is marked.
"""

from __future__ import annotations

from collections.abc import Iterator

from zombiescan import helpers
from zombiescan.azure import ArmError
from zombiescan.cleaners import Step, cleaner, snapshot_step
from zombiescan.models import Finding, ScanContext


def _delete(finding: Finding, resource_type: str, what: str, irreversible: bool = False) -> Step:
    """The one-call plan that most findings have."""
    return Step(
        description=f"delete {what} {finding.resource_id}",
        method="DELETE",
        path=finding.arm_id,
        resource_type=resource_type,
        irreversible=irreversible,
    )


# --------------------------------------------------------------------------
# Compute
# --------------------------------------------------------------------------


@cleaner("unattached-disk")
def clean_unattached_disk(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Snapshot the disk, then delete it.

    The snapshot comes first and the runner aborts the finding at the first
    failure, so a snapshot that does not complete can never be followed by the
    delete that assumed it. An incremental snapshot of a disk that has been
    snapshotted before costs very little; of one that has not, it costs the
    cheapest snapshot rate for the data actually on it -- either way less than
    the disk it replaces.
    """
    yield snapshot_step(finding, finding.arm_id, finding.location)
    yield _delete(finding, "Microsoft.Compute/disks", "managed disk")


@cleaner("deallocated-vm")
def clean_deallocated_vm(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the VM and leave its disks behind.

    ARM's default is to detach rather than delete a managed disk, so the OS
    and data disks survive the VM. That is deliberate: it makes this step
    reversible in the way that matters, and the unattached-disk check reports
    those disks on the next scan with a snapshot-first plan of their own.

    The NIC survives too, and ``orphaned-nic`` picks it up.
    """
    yield _delete(finding, "Microsoft.Compute/virtualMachines", "virtual machine")


@cleaner("orphaned-snapshot")
def clean_orphaned_snapshot(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the snapshot.

    Irreversible: a snapshot is itself the backup, so there is nothing to take
    a copy of it into and no recovery window afterwards. Its source disk is
    already gone -- that is what made it a finding -- so this is the last copy
    of whatever was on that disk.
    """
    yield _delete(finding, "Microsoft.Compute/snapshots", "snapshot", irreversible=True)


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------


@cleaner("unused-public-ip")
def clean_unused_public_ip(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Release the address.

    Irreversible: Azure returns the address to the regional pool and will not
    hand the same one back. Anything with it in a DNS record, a partner's
    allow-list or a firewall rule elsewhere stops working, and no amount of
    re-creating the resource gets the address back.
    """
    yield _delete(
        finding, "Microsoft.Network/publicIPAddresses", "public IP address", irreversible=True
    )


@cleaner("idle-nat-gateway")
def clean_idle_nat_gateway(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the gateway.

    Not irreversible: a NAT gateway holds no data and can be recreated from
    its configuration. The public IP addresses it held survive and are
    reported by ``unused-public-ip`` on the next scan -- deleting them here
    would release addresses the operator may want to keep.
    """
    yield _delete(finding, "Microsoft.Network/natGateways", "NAT gateway")


@cleaner("idle-load-balancer")
def clean_idle_load_balancer(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the load balancer.

    Its frontend public IP addresses survive and are reported separately, for
    the same reason as the NAT gateway's: releasing an address is the one step
    here that cannot be undone, and it should be an explicit decision.
    """
    yield _delete(finding, "Microsoft.Network/loadBalancers", "load balancer")


@cleaner("orphaned-nic")
def clean_orphaned_nic(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the network interface.

    This is usually the step that unblocks the rest: the public IP, subnet and
    virtual network the NIC was pinning all become deletable once it is gone.
    Nothing is lost -- a NIC holds configuration, not data.
    """
    yield _delete(finding, "Microsoft.Network/networkInterfaces", "network interface")


@cleaner("unused-nsg")
def clean_unused_nsg(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the security group.

    Its rules go with it. That is recoverable in practice rather than in the
    API: Azure's Activity Log keeps the resource's last written state for
    ninety days, so the rule set can be read back out of it. The finding's
    details also record the rule names.
    """
    yield _delete(finding, "Microsoft.Network/networkSecurityGroups", "network security group")


@cleaner("unused-subnet")
def clean_unused_subnet(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the subnet, freeing its address range.

    ARM refuses this outright if anything has moved into the subnet since the
    scan, which is the right failure: the range is only free to reuse if
    nothing is in it, and Azure is the authority on that at the moment of the
    call rather than at the moment of the scan.
    """
    yield _delete(finding, "Microsoft.Network/virtualNetworks/subnets", "subnet")


@cleaner("unused-dns-zone")
def clean_unused_dns_zone(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the zone.

    Not irreversible in terms of data -- the zone holds only the SOA and NS
    records Azure created -- but recreating it later assigns a **different**
    set of name servers, so the delegation at the registrar has to be updated
    again. A zone with records in it is not a finding, so nothing that is
    currently resolving depends on these.
    """
    yield _delete(finding, "Microsoft.Network/dnszones", "DNS zone")


# --------------------------------------------------------------------------
# Platform services
# --------------------------------------------------------------------------


@cleaner("idle-app-service-plan")
def clean_idle_app_service_plan(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the plan.

    ARM refuses while any app is still on it, which is the check that matters:
    an app deployed between the scan and the apply keeps its plan.
    """
    yield _delete(finding, "Microsoft.Web/serverfarms", "App Service plan")


@cleaner("paused-sql-database")
def clean_paused_sql_database(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the database.

    Not irreversible: Azure keeps a deleted database's point-in-time backups
    for the server's retention period -- seven days by default, up to
    thirty-five -- and it can be restored from
    ``restorableDroppedDatabases`` until then. Past that window it is gone, so
    this is a recovery *window* rather than a recovery guarantee, and the
    window is what the flag is about.
    """
    yield _delete(finding, "Microsoft.Sql/servers/databases", "SQL database")


@cleaner("disabled-key-vault-key")
def clean_disabled_key_vault_key(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the key.

    Not irreversible: Key Vault soft-delete is mandatory and keeps a deleted
    key recoverable for the vault's retention period, seven to ninety days.
    The key keeps being billed for that period, so the saving starts when the
    window closes rather than when this returns -- the same shape as a
    destroyed KMS key version on Google Cloud, and worth knowing before
    anyone checks next month's invoice for the difference.
    """
    yield Step(
        description=(
            f"delete key {finding.resource_id} (recoverable during the vault's "
            "soft-delete retention period)"
        ),
        method="DELETE",
        path=finding.arm_id,
        resource_type="Microsoft.KeyVault/vaults",
    )


@cleaner("empty-container-registry")
def clean_empty_container_registry(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the registry.

    It holds no images -- that is what made it a finding -- so there is
    nothing to back up. The registry name is released and becomes available
    to anyone, which matters only if something still pulls by that login
    server, and nothing in an empty registry can be pulled.
    """
    yield _delete(finding, "Microsoft.ContainerRegistry/registries", "container registry")


@cleaner("unused-availability-test")
def clean_unused_availability_test(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the web test.

    The alert rule that watches it is a separate resource and survives. It
    will stop firing, because there is no longer a test to fail, but it is
    left in place rather than deleted -- an alert rule can watch more than one
    test, and removing one that still has work to do is a worse outcome than
    leaving a quiet rule behind.
    """
    yield _delete(finding, "Microsoft.Insights/webtests", "availability test")


@cleaner("empty-resource-group")
def clean_empty_resource_group(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the group, after checking again that it is still empty.

    ``az group delete`` is the most destructive command in Azure: it removes
    everything inside the group, without listing what that was. The whole
    basis of this finding is that there is nothing inside, and a scan is a
    snapshot -- something can be deployed into the group between the scan and
    the apply, and then this step would delete it.

    So the group is listed again here, during planning, and the plan is
    refused if anything has appeared. That is a read call, which planning is
    allowed to make, and it is the difference between a safe command and the
    worst thing this tool could do.
    """
    try:
        contents = list(
            ctx.arm.list(
                f"/subscriptions/{finding.subscription}/resourceGroups/"
                f"{finding.resource_id}/resources",
                "Microsoft.Resources/resourceGroups",
            )
        )
    except ArmError as exc:
        raise RuntimeError(
            f"could not re-check that resource group {finding.resource_id} is empty "
            f"({helpers.arg(str(exc))}); refusing to plan a group delete without it"
        ) from exc

    if contents:
        names = ", ".join(str(item.get("name")) for item in contents[:5])
        raise RuntimeError(
            f"resource group {finding.resource_id} is no longer empty -- it now holds "
            f"{len(contents)} resource(s) ({names}). Re-run the scan"
        )

    yield Step(
        description=f"delete empty resource group {finding.resource_id}",
        method="DELETE",
        path=f"/subscriptions/{finding.subscription}/resourceGroups/{finding.resource_id}",
        resource_type="Microsoft.Resources/resourceGroups",
    )
