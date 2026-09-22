"""The core pack: the Azure services almost every subscription uses.

Compute, Networking, Storage, SQL Database, Key Vault, Container Registry,
App Service, Log Analytics and Resource Manager itself. Anything here is
waste that shows up on a first scan of a subscription nobody has audited.

Checks are discovered by existing -- ``import_pack_modules`` imports every
module in this directory, ``cleaners`` included -- so adding a check is adding
a file, not editing an import list that would eventually drift. Core's rate
specs are the ones in ``zombiescan.pricing.rates``; a pack that prices
something core has never heard of ships a ``rates.py`` of its own, the way
``aks`` does.
"""

from __future__ import annotations

from zombiescan.packs import import_pack_modules, register_pack

register_pack(
    "core",
    version="0.1.0",
    description="Compute, Networking, Storage, SQL, Key Vault, Container Registry, App Service",
)

import_pack_modules(__name__, list(__path__))
