"""Core data types shared by every check.

Costs are estimates. They are computed from a bundled price table rather than
from your actual bill, so they will not match Azure Cost Management to the
cent. A finding marked ``approximate_cost`` fell back to the default region
because the table has no entry for its own.

Three fields differ from what an AWS scanner would carry. ``subscription`` is
the account-equivalent, and scanning several at once is the normal case.
``resource_group`` is Azure's own doing: no ``az`` delete command works
without it, so a finding that did not carry one would produce a remediation
nobody could run. And ``arm_id`` keeps the full resource path, which is the
only identifier unique across a tenant and the handle every cleaner acts on --
``resource_id`` holds the bare name because that is what a report should show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from zombiescan import azure

if TYPE_CHECKING:  # pragma: no cover
    from zombiescan.azure import Arm
    from zombiescan.pricing import PriceTable


@dataclass(frozen=True)
class Finding:
    """One resource that appears to be waste."""

    check: str
    resource_id: str
    resource_type: str
    subscription: str
    location: str
    reason: str
    monthly_cost: float
    remediation: str
    resource_group: str = ""
    arm_id: str = ""
    approximate_cost: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def region(self) -> str:
        """The region this finding is priced in."""
        return azure.region_of(self.location)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        """Rebuild a finding from --json output.

        Lets `clean --from report.json` act on exactly the findings someone
        reviewed, rather than on whatever a fresh scan happens to see now.
        """
        return cls(
            check=data["check"],
            resource_id=data["resource_id"],
            resource_type=data.get("resource_type", "unknown"),
            subscription=data.get("subscription", "unknown"),
            location=data.get("location", "unknown"),
            reason=data.get("reason", ""),
            monthly_cost=float(data.get("monthly_cost", 0.0)),
            remediation=data.get("remediation", ""),
            resource_group=data.get("resource_group", ""),
            arm_id=data.get("arm_id", ""),
            approximate_cost=bool(data.get("approximate_cost", False)),
            details=data.get("details") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "subscription": self.subscription,
            "resource_group": self.resource_group,
            "location": self.location,
            "arm_id": self.arm_id,
            "reason": self.reason,
            "monthly_cost": round(self.monthly_cost, 2),
            "approximate_cost": self.approximate_cost,
            "remediation": self.remediation,
            "details": self.details,
        }


@dataclass
class ScanContext:
    """What a check gets handed: a subscription, ARM, and the price table.

    There is no region here. One Resource Graph query covers every region and
    every resource group a subscription has, so a check runs once per
    subscription and reads each finding's location off the resource.

    ``providers`` is the set of resource provider namespaces registered on
    this subscription. The engine has already refused to run a check whose
    provider is missing, so a check only needs it to ask about a *second*
    service it reads opportunistically.
    """

    arm: Arm
    subscription: str
    pricing: PriceTable
    providers: frozenset[str] = frozenset()

    @property
    def scope(self) -> str:
        """``/subscriptions/<id>``, the prefix every ARM path starts from."""
        return f"/subscriptions/{self.subscription}"

    # Resource Manager's own collections are not addressed through
    # ``/providers/``. A resource group lives at ``/subscriptions/<id>/resourcegroups``
    # and answers 404 at the path every other type uses, which is easy to read
    # as "nothing here" rather than as the wrong URL.
    _CONTROL_PLANE = {"Microsoft.Resources/resourceGroups": "resourcegroups"}

    def provider_path(self, resource_type: str) -> str:
        """The subscription-wide list path for a resource type.

        ``provider_path("Microsoft.Compute/disks")`` is
        ``/subscriptions/<id>/providers/Microsoft.Compute/disks``, which lists
        every disk in every resource group in one call.
        """
        direct = self._CONTROL_PLANE.get(resource_type)
        if direct:
            return f"{self.scope}/{direct}"
        namespace, _, rest = resource_type.partition("/")
        return f"{self.scope}/providers/{namespace}/{rest}"

    def list(self, resource_type: str, key: str = "value", **query: str):
        """Every resource of one type in this subscription, across all groups."""
        return self.arm.list(self.provider_path(resource_type), resource_type, key=key, **query)

    def graph(self, query: str):
        """Every row of a Resource Graph query scoped to this subscription."""
        return self.arm.graph(query, self.subscription)

    def has_provider(self, namespace: str) -> bool:
        """Whether a resource provider is registered here.

        Worth asking before reading a *second* service, because ARM answers an
        unregistered provider's list call with an empty page rather than an
        error -- so a cross-reference against a service nobody enabled would
        silently look like "nothing references this".
        """
        return namespace.lower() in {p.lower() for p in self.providers}
