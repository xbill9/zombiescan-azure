"""How GKE findings are removed."""

from __future__ import annotations

from collections.abc import Iterator

from zombiescan.cleaners import Step, cleaner
from zombiescan.models import Finding, ScanContext


@cleaner("gke-idle-cluster")
def clean_idle_cluster(ctx: ScanContext, finding: Finding) -> Iterator[Step]:
    """Delete the cluster.

    Irreversible: the control plane and every object stored in it -- every
    Deployment, Secret and ConfigMap -- go with it, and GKE keeps no copy.
    Persistent volumes provisioned by the cluster survive as disks, and the
    unattached-disk check reports them on the next scan.
    """
    name = finding.resource_id
    yield Step(
        description=f"delete GKE cluster {name} and its control plane state",
        api="container",
        operation="projects.locations.clusters.delete",
        params={
            "name": (f"projects/{finding.project}/locations/{finding.location}/clusters/{name}")
        },
        irreversible=True,
    )
