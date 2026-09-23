#!/usr/bin/env python3
"""Every figure the article derives by arithmetic, computed here and not by hand.

Rates come from the bundled price table (eastus, 730 hours to the month). The
AWS and Google Cloud figures come from the evidence of the other two articles
in the series, read from their files.

    python3 articles/zombiescan-azure/derive-figures.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
HOME = pathlib.Path.home()
sys.path.insert(0, str(REPO / "src"))
from zombiescan.helpers import disk_tier  # noqa: E402

TABLE = json.loads((REPO / "src/zombiescan/pricing/table.json").read_text())
HOURS = TABLE["hours_per_month"]
R = "eastus"


def line(label: str, value: float, how: str) -> None:
    print(f"{label:58} ${value:,.2f}   ({how})")


print(f"price table generated {TABLE['_meta']['generated']}, region {R}, {HOURS} h/month\n")

print("== Azure managed disks, flat per tier")
tiers = TABLE["disk_tier_month"][R]
for tier in ("P1 LRS", "P4 LRS", "P10 LRS", "P10 ZRS", "P30 LRS", "P80 LRS", "S4 LRS", "E10 LRS"):
    line(f"{tier} per month", tiers[tier], "table")
for label, sku, size in (
    ("1 GiB Premium SSD", "Premium_LRS", 1),
    ("65 GiB Premium SSD", "Premium_LRS", 65),
    ("100 GiB Premium SSD", "Premium_LRS", 100),
    ("128 GiB Premium SSD", "Premium_LRS", 128),
    ("4 GiB Standard HDD", "Standard_LRS", 4),
    ("32 GiB Standard HDD", "Standard_LRS", 32),
):
    tier = disk_tier(sku, size)
    line(f"{label} bills as {tier}", tiers[tier], "helpers.disk_tier")
line("Premium SSD v2 per GiB-month", TABLE["disk_gb_month"][R]["PremiumV2_LRS"], "table")

print("\n== Raw rates as stored in the table")
for label, value in (
    ("public_ip_hour", TABLE["public_ip_hour"][R]),
    ("nat_gateway_hour (global)", TABLE["nat_gateway_hour"]["_value"]),
    ("load_balancer_hour (global)", TABLE["load_balancer_hour"]["_value"]),
    ("aks_cluster_hour Standard", TABLE["aks_cluster_hour"][R]["Standard"]),
    ("dedicated_host_hour dsv3type3", TABLE["dedicated_host_hour"][R]["dsv3type3"]),
    ("vm_hour Standard_D2s_v3", TABLE["vm_hour"][R]["Standard_D2s_v3"]),
    ("container_apps_hour dedicated_management", TABLE["container_apps_hour"][R]["dedicated_management"]),
):
    print(f"{label:58} ${value}")

print("\n== Hourly rates at 730 hours")
line(
    "Standard static public IP per month",
    TABLE["public_ip_hour"][R] * HOURS,
    "public_ip_hour x 730",
)
line("NAT Gateway per month", TABLE["nat_gateway_hour"]["_value"] * HOURS, "global rate x 730")
line(
    "Standard Load Balancer rules per month",
    TABLE["load_balancer_hour"]["_value"] * HOURS,
    "global rate x 730",
)
line(
    "AKS Standard control plane per month",
    TABLE["aks_cluster_hour"][R]["Standard"] * HOURS,
    "x 730",
)
line("AKS Free control plane per month", TABLE["aks_cluster_hour"][R]["Free"] * HOURS, "x 730")
line(
    "Dedicated host DSv3-Type3 per month",
    TABLE["dedicated_host_hour"][R]["dsv3type3"] * HOURS,
    "x 730",
)
line("Standard_D2s_v3 Linux per month", TABLE["vm_hour"][R]["Standard_D2s_v3"] * HOURS, "x 730")

ptu = TABLE["ptu_hour"][R]
for sku in ("ProvisionedManaged", "DataZoneProvisionedManaged", "GlobalProvisionedManaged"):
    print(f"{sku + ' per PTU-hour':58} ${ptu[sku]:,.2f}")
line(
    "15 PTU ProvisionedManaged per month", 15 * ptu["ProvisionedManaged"] * HOURS, "15 x rate x 730"
)

aca = TABLE["container_apps_hour"][R]
line(
    "Idle replica 0.5 vCPU / 1 GiB per month",
    (0.5 * aca["idle_vcpu"] + 1 * aca["idle_gib"]) * HOURS,
    "(0.5 idle_vcpu + 1 idle_gib) x 730",
)
line(
    "Dedicated D4 instance (4 vCPU, 16 GiB) per month",
    (4 * aca["dedicated_vcpu"] + 16 * aca["dedicated_gib"]) * HOURS,
    "(4 vcpu + 16 gib) x 730",
)
line("Dedicated plan management fee per month", aca["dedicated_management"] * HOURS, "x 730")

print("\n== The other two clouds, from their articles' evidence")
aws = json.loads((HOME / "zombiescan/articles/zombiescan/evidence/prices.json").read_text())[
    "us-east-1"
]
print(f"{'AWS gp3 per GB-month (us-east-1)':58} ${aws['ebs_gp3_gb_month']:.3f}")
print(f"{'AWS NAT gateway per month (us-east-1)':58} ${aws['nat_gateway_month']:,.2f}")
gcp_text = (HOME / "zombiescan-gcp/articles/zombiescan-gcp/evidence/prices.txt").read_text()
gcp_balanced = float(
    re.search(r"disk_gb_month\.us-central1\.pd-balanced = ([\d.]+)", gcp_text).group(1)
)
print(f"{'GCP pd-balanced per GB-month (us-central1)':58} ${gcp_balanced:.3f}")
line("AWS gp3, 128 GB", 128 * aws["ebs_gp3_gb_month"], "128 x per-GB")
line("GCP pd-balanced, 128 GB", 128 * gcp_balanced, "128 x per-GB")
line("AWS gp3, 1 GB", 1 * aws["ebs_gp3_gb_month"], "1 x per-GB")
line("Azure Premium SSD, 1 GiB (P1)", tiers["P1 LRS"], "tier")

print("\n== research-mesh-rg, from evidence/cost-management.json (Azure's own totals)")
cost = json.loads((HERE / "evidence/cost-management.json").read_text())["data"]
total = cost["last_30_days_total"]["rows"][0]["Cost"]
print(f"{'Last 30 days, whole group':58} ${total:,.2f}")
for row in cost["last_30_days_by_resource"]["rows"]:
    name = row["ResourceId"].rsplit("/", 1)[-1]
    print(f"{'Last 30 days, ' + name:58} ${row['Cost']:,.4f}")
since = cost["since_2026_08_13_by_meter"]["rows"]
for category in sorted({r["MeterCategory"] for r in since}):
    subtotal = sum(r["Cost"] for r in since if r["MeterCategory"] == category)
    print(f"{'Since 2026-08-13, ' + category:58} ${subtotal:,.2f}")
print(f"{'Registry share of last 30 days':58} "
      f"{next(r['Cost'] for r in cost['last_30_days_by_resource']['rows'] if 'registries' in r['ResourceId']) / total:.1%}")
print(f"{'Registry, a year at the last 30 days rate':58} "
      f"${next(r['Cost'] for r in cost['last_30_days_by_resource']['rows'] if 'registries' in r['ResourceId']) * 365 / 30:,.2f}")
