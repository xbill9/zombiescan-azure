"""The core pack: the Google Cloud services almost every project uses.

Compute Engine, Cloud SQL, Cloud Storage, Cloud DNS, KMS, Secret Manager,
Artifact Registry, Filestore and Cloud Logging. Anything here is waste that
shows up on a first scan of a project nobody has audited.

Checks are discovered by existing -- ``import_pack_modules`` imports every
module in this directory, ``cleaners`` included -- so adding a check is adding
a file, not editing an import list that would eventually drift. Core's rate
specs are the ones in ``zombiescan.pricing.rates``; a pack that prices
something core has never heard of ships a ``rates.py`` of its own, the way
``gke`` does.
"""

from __future__ import annotations

from zombiescan.packs import import_pack_modules, register_pack

register_pack(
    "core",
    version="0.1.0",
    description="Compute Engine, Cloud SQL, Storage, DNS, KMS, Secrets, Artifact Registry",
)

import_pack_modules(__name__, list(__path__))
