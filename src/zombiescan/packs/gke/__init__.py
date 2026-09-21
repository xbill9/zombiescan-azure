"""The GKE pack: Google Kubernetes Engine.

Extracted from core as the proof that the pack seam carries a whole service --
its own API (``container.googleapis.com``), its own rate section, and its own
fetcher against a SKU family core never looks at.

The headline trap is ``gke-idle-cluster``: a GKE cluster bills a flat
management fee per hour whether or not a single pod runs on it. Scaling every
node pool to zero removes the node cost and leaves that fee untouched, so
"scale it down to save money" saves the nodes and nothing else.
"""

from __future__ import annotations

from zombiescan.packs import import_pack_modules, register_pack

register_pack(
    "gke",
    version="0.1.0",
    description="Google Kubernetes Engine clusters and node pools",
)

import_pack_modules(__name__, list(__path__))
