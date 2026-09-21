"""Core data types shared by every check.

Costs are estimates. They are computed from a bundled price table rather than
from your actual bill, so they will not match the Cloud Billing console to the
cent. A finding marked ``approximate_cost`` fell back to the default region
because the table has no entry for its own.

Two fields differ from what an AWS scanner would carry. ``location`` holds a
zone, a region or ``global``, because Google Cloud resources come in all three
shapes and the operator needs the exact one to delete anything. ``project`` is
the account-equivalent: a resource id is unique inside a project and nowhere
else, and scanning several projects at once is the normal case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from zombiescan import gcp

if TYPE_CHECKING:  # pragma: no cover
    from zombiescan.gcp import Clients
    from zombiescan.pricing import PriceTable


@dataclass(frozen=True)
class Finding:
    """One resource that appears to be waste."""

    check: str
    resource_id: str
    resource_type: str
    project: str
    location: str
    reason: str
    monthly_cost: float
    remediation: str
    approximate_cost: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def region(self) -> str:
        """The region this finding is priced in. A zone prices as its region."""
        return gcp.region_of(self.location)

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
            project=data.get("project", "unknown"),
            location=data.get("location", "unknown"),
            reason=data.get("reason", ""),
            monthly_cost=float(data.get("monthly_cost", 0.0)),
            remediation=data.get("remediation", ""),
            approximate_cost=bool(data.get("approximate_cost", False)),
            details=data.get("details") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "project": self.project,
            "location": self.location,
            "reason": self.reason,
            "monthly_cost": round(self.monthly_cost, 2),
            "approximate_cost": self.approximate_cost,
            "remediation": self.remediation,
            "details": self.details,
        }


@dataclass
class ScanContext:
    """What a check gets handed: a project, its clients, and the price table.

    There is no region here. Compute Engine's ``aggregatedList`` and the
    ``locations/-`` wildcard let one call cover every location, so a check runs
    once per project and reads each finding's location off the resource.
    """

    clients: Clients
    project: str
    pricing: PriceTable

    def client(self, api: str) -> Any:
        """The discovery client for one API, built once per scan."""
        return self.clients.get(api)

    @property
    def parent(self) -> str:
        """``projects/<id>``, the prefix most non-Compute APIs want."""
        return f"projects/{self.project}"

    def any_location(self, api_path: str = "locations") -> str:
        """``projects/<id>/locations/-``: every location, in one call."""
        return f"{self.parent}/{api_path}/{gcp.ANY_LOCATION}"
