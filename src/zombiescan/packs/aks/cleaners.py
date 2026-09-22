"""How AKS findings are removed."""

from __future__ import annotations

from collections.abc import Iterator

from zombiescan.cleaners import Step, cleaner
from zombiescan.models import Finding, ScanContext


@cleaner("aks-idle-cluster")
def clean_idle_cluster(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the cluster.

    Irreversible: the control plane and every object stored in it -- every
    Deployment, Secret and ConfigMap -- go with it, and AKS keeps no copy.
    Persistent volumes provisioned by the cluster survive as managed disks in
    the node resource group, and the unattached-disk check reports them on the
    next scan.
    """
    yield Step(
        description=(f"delete AKS cluster {finding.resource_id} and its control plane state"),
        method="DELETE",
        path=finding.arm_id,
        resource_type="Microsoft.ContainerService/managedClusters",
        irreversible=True,
    )
