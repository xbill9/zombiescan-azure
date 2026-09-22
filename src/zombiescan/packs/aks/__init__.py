"""The AKS pack: Azure Kubernetes Service.

Kept separate from core as the proof that the pack seam carries a whole
service -- its own resource provider (``Microsoft.ContainerService``), its own
rate section, and its own fetcher against a meter core never looks at.

The headline finding is ``aks-idle-cluster``, and what it reports is the
opposite of the equivalent on Google Cloud. A GKE cluster bills a flat
management fee per hour whatever runs on it, so an empty one is expensive. An
AKS cluster on the **Free** tier bills nothing at all for its control plane,
so an empty Free-tier cluster is genuinely free -- and only the Standard and
Premium tiers carry a per-hour fee. The check reports both and prices each at
what its own tier costs, rather than quoting one number that is wrong for most
clusters.
"""

from __future__ import annotations

from zombiescan.packs import import_pack_modules, register_pack

register_pack(
    "aks",
    version="0.1.0",
    description="Azure Kubernetes Service clusters and node pools",
)

import_pack_modules(__name__, list(__path__))
