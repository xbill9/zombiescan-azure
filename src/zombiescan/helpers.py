"""Helpers shared by more than one check."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import re
import shlex
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from zombiescan import azure
from zombiescan.azure import ArmError
from zombiescan.models import ScanContext

# ARM writes the fractional second to seven digits -- "2026-01-02T03:04:05.1234567Z"
# -- which is one more than `fromisoformat` accepts on the oldest Python this
# supports. Trimming it to microseconds costs nothing at day resolution.
_SUBSECOND = re.compile(r"\.(\d{1,9})")


def age_days(timestamp: str | None) -> int | None:
    """Whole days since an ISO 8601 timestamp, or None if it is absent.

    ARM returns timestamps as strings rather than as datetimes, some ending in
    ``Z`` and some carrying a numeric offset. Both parse once the suffix is
    spelled the way ``fromisoformat`` expects and the fractional second is cut
    to six digits.
    """
    if not timestamp:
        return None
    text = _SUBSECOND.sub(lambda m: "." + m.group(1)[:6], str(timestamp).strip())
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - parsed).days


def epoch_age_days(seconds: Any) -> int | None:
    """Whole days since a Unix timestamp, or None if it is absent.

    Key Vault reports ``attributes.created`` and ``attributes.updated`` as
    epoch seconds rather than as strings, alone among the services scanned
    here.
    """
    try:
        moment = dt.datetime.fromtimestamp(float(seconds), dt.UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return (dt.datetime.now(dt.UTC) - moment).days


def gb(value: Any) -> float:
    """A size ARM reports as a number or a string of GB, as a float."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def bytes_to_gb(value: Any) -> float:
    """Bytes as GB, using the 2^30 definition Azure bills storage by."""
    return gb(value) / (1024**3)


# --------------------------------------------------------------------------
# Managed disks
# --------------------------------------------------------------------------
#
# Azure prices a managed disk by the *tier* its provisioned size falls into,
# not by the gigabyte. A 1 GiB Premium SSD and a 128 GiB one are both a P10
# and both cost the same: what you pay for is the next size up the ladder from
# whatever you asked for. Only Premium SSD v2 and Ultra bill per provisioned
# GiB, and both also bill provisioned IOPS and throughput separately.
#
# So pricing a disk means finding its rung on the ladder, and a per-GB
# calculation -- the right answer on every other cloud -- understates a small
# disk by up to two orders of magnitude. A 4 GiB Premium disk is not 4 x a
# per-GB rate; it is a P1, and it has a price of its own.

# Provisioned size in GiB -> tier suffix, for the SKU families that bill by
# tier. Azure rounds *up* to the first rung that fits.
_DISK_LADDER = (
    (4, "1"),
    (8, "2"),
    (16, "3"),
    (32, "4"),
    (64, "6"),
    (128, "10"),
    (256, "15"),
    (512, "20"),
    (1024, "30"),
    (2048, "40"),
    (4096, "50"),
    (8192, "60"),
    (16384, "70"),
    (32767, "80"),
)

# The tier letter and the redundancy each SKU is published under. The price
# table is keyed by the two together -- "P10 LRS", "P10 ZRS" -- because zone
# redundancy costs about half as much again and is a property of the disk, not
# of the region.
_TIER_SKUS = {
    "Standard_LRS": ("S", "LRS"),
    "StandardSSD_LRS": ("E", "LRS"),
    "StandardSSD_ZRS": ("E", "ZRS"),
    "Premium_LRS": ("P", "LRS"),
    "Premium_ZRS": ("P", "ZRS"),
}

# Standard HDD has no rung below S4: a 4 GiB Standard HDD disk is provisioned
# and billed as a 32 GiB S4. The SSD families do start at 4 GiB, so this is a
# property of the family rather than of the ladder.
_LOWEST_RUNG = {"S": "4"}


def disk_sku(disk: dict[str, Any]) -> str:
    """A disk's SKU name, e.g. ``Premium_LRS``."""
    return (disk.get("sku") or {}).get("name") or "Standard_LRS"


def disk_tier(sku: str, size_gb: float) -> str | None:
    """The billed tier for a disk, e.g. ``P10 LRS``, or None if it bills per GiB.

    Azure rounds up: a 5 GiB Premium disk is provisioned as a P2 and billed as
    one. A disk larger than the top rung is billed at the top rung, and one
    smaller than its family's smallest rung is billed at that.
    """
    family = _TIER_SKUS.get(sku)
    if family is None:
        return None
    prefix, redundancy = family
    floor = _LOWEST_RUNG.get(prefix)
    rungs = list(_DISK_LADDER)
    if floor is not None:
        start = next(i for i, (_, suffix) in enumerate(rungs) if suffix == floor)
        rungs = rungs[start:]
    for ceiling, suffix in rungs:
        if size_gb <= ceiling:
            return f"{prefix}{suffix} {redundancy}"
    return f"{prefix}{rungs[-1][1]} {redundancy}"


def disk_monthly_cost(ctx: ScanContext, disk: dict[str, Any], region: str) -> tuple[float, bool]:
    """What one managed disk costs a month, and whether the figure is a fallback.

    Tiered SKUs price by the rung; Premium SSD v2 and Ultra price by the
    gigabyte. Both go through the price table, so neither is a number written
    into a check.
    """
    size_gb = gb((disk.get("properties") or disk).get("diskSizeGB"))
    sku = disk_sku(disk)
    tier = disk_tier(sku, size_gb)
    if tier is None:
        price, approximate = ctx.pricing.rate("disk.gb_month", region=region, variant=sku)
        return size_gb * price, approximate
    price, approximate = ctx.pricing.rate("disk.tier_month", region=region, variant=tier)
    return price, approximate


PUBLIC_IPS = "Microsoft.Network/publicIPAddresses"


def public_ip_monthly_cost(ctx: ScanContext, address: dict[str, Any]) -> tuple[float, bool]:
    """What one public IP address costs a month, and whether the figure is a fallback.

    A Standard address bills the same hourly rate attached or idle. A dynamic
    Basic address is billed only while attached to something running, so a
    detached or deallocated one costs nothing. An address carved from a public
    IP prefix is priced at nothing too: the prefix bills per address in its
    range from the moment it exists, so its cost belongs to the prefix.
    """
    props = properties(address)
    if (props.get("publicIPPrefix") or {}).get("id"):
        return 0.0, False
    sku = (address.get("sku") or {}).get("name") or "Basic"
    allocation = props.get("publicIPAllocationMethod") or "Static"
    if sku == "Basic" and allocation == "Dynamic":
        return 0.0, False
    return ctx.pricing.rate("public_ip.month", region=azure.region_of(location_of(address)))


def public_ips_by_id(ctx: ScanContext) -> dict[str, dict[str, Any]]:
    """Every public IP address in the subscription, keyed by lowercased ARM id."""
    return {
        str(address.get("id") or "").lower(): address
        for address in ctx.list(PUBLIC_IPS)
        if address.get("id")
    }


def held_public_ips(
    ctx: ScanContext, ids: Iterable[str], addresses: dict[str, dict[str, Any]]
) -> tuple[float, bool, list[dict[str, Any]]]:
    """What the public IPs a zombie holds cost a month, and what they are.

    An address held by an orphaned NIC, a deallocated VM or an idle load
    balancer points its ``ipConfiguration`` at that holder, so
    ``unused-public-ip`` counts it as in use. The holder's finding carries its
    cost instead, or nothing would.
    """
    total = 0.0
    approximate = False
    held: list[dict[str, Any]] = []
    for arm_id in sorted({i.lower() for i in ids if i}):
        address = addresses.get(arm_id)
        if address is None:
            continue
        cost, is_approximate = public_ip_monthly_cost(ctx, address)
        total += cost
        approximate = approximate or (is_approximate and bool(cost))
        held.append(
            {
                "name": address.get("name"),
                "ip_address": properties(address).get("ipAddress"),
                "sku": (address.get("sku") or {}).get("name") or "Basic",
                "monthly_cost": round(cost, 2),
            }
        )
    return total, approximate, held


# --------------------------------------------------------------------------
# Cross-referencing
# --------------------------------------------------------------------------


def arm_ids(references: Iterable[Any]) -> set[str]:
    """The lowercased ``id`` of every ARM reference in ``references``.

    Azure expresses "what is using this" as a list of ``{"id": "/subscriptions/..."}``
    objects, and the case ARM returns an id in is not the case Resource Graph
    returns it in -- comparing them as written finds nothing and reports
    everything as unused. Lowercasing both sides is the only safe comparison.
    """
    found = set()
    for reference in references or []:
        value = reference.get("id") if isinstance(reference, dict) else reference
        if isinstance(value, str) and value:
            found.add(value.lower())
    return found


def properties(resource: dict[str, Any]) -> dict[str, Any]:
    """A resource's ``properties`` block, whatever answered for it.

    ARM nests a resource's state under ``properties``; a Resource Graph query
    that projects specific fields returns them at the top level. A check
    written against one shape and fed the other silently finds nothing, so
    both go through here.
    """
    nested = resource.get("properties")
    return nested if isinstance(nested, dict) else resource


def across_parents(
    fetch: Callable[[str], list[Any]],
    parents: Iterable[str],
    workers: int = 12,
) -> Iterator[tuple[str, Any]]:
    """Run ``fetch`` against every parent resource, yielding ``(parent, item)``.

    Most of Azure lists subscription-wide, so this is rarely needed. Where a
    provider only offers a per-parent list -- Key Vault's keys and secrets are
    the case in this repository -- the calls are made in parallel rather than
    in series, because a subscription with sixty vaults would otherwise spend
    a minute on one check.

    A parent that refuses the call contributes nothing rather than failing the
    check: a vault whose network rules exclude the scanner is ordinary, and one
    unreachable vault must not cost the findings from the other fifty-nine.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, parent): parent for parent in parents}
        for future in concurrent.futures.as_completed(futures):
            parent = futures[future]
            try:
                items = future.result()
            except ArmError:
                continue
            for item in items:
                yield parent, item


# --------------------------------------------------------------------------
# Generated commands
# --------------------------------------------------------------------------
#
# Every remediation string is an `az` command the operator can paste. Three
# things have to be true of all of them, and `tests/test_report.py` checks all
# three at the source:
#
#   * it names its subscription, so pasting one into a shell pointed at a
#     different subscription deletes nothing by surprise;
#   * every interpolated resource id goes through `arg`;
#   * it does not stop to ask a question.
#
# That last one is where Azure differs from Google. `gcloud` takes `--quiet`
# on everything. `az` has no global equivalent: `--yes` exists only on the
# commands that would otherwise prompt, and passing it to one that would not
# is an error rather than a no-op -- `az network nic delete --yes` fails
# outright. So the commands that accept it are named here.

# Verified against `az <command> --help` on Azure CLI 2.90. `make verify-az`
# re-checks this set against the installed CLI; `tests/test_live.py` does the
# same under ZOMBIESCAN_LIVE=1, because the right list is whatever the
# operator's `az` actually accepts, not what this comment remembers.
CONFIRMS = frozenset(
    {
        "acr delete",
        "aks delete",
        "appservice plan delete",
        "disk delete",
        "group delete",
        "monitor log-analytics workspace delete",
        "network dns zone delete",
        "sql db delete",
        "storage account delete",
        "vm delete",
    }
)


def _prompts(command: str) -> bool:
    """Whether this ``az`` command would stop and ask, so needs ``--yes``."""
    words = command.split()
    if words and words[0] == "az":
        words = words[1:]
    # Match on the longest leading run of flag-free words, which is the
    # command itself; everything after the first `--` is arguments.
    verb: list[str] = []
    for word in words:
        if word.startswith("-"):
            break
        verb.append(word)
    return " ".join(verb) in CONFIRMS


def az(command: str, subscription: str, resource_group: str = "") -> str:
    """A complete, non-interactive ``az`` command.

    Every generated command carries its subscription explicitly, and its
    resource group where the command takes one, so it means the same thing
    wherever it is pasted. ``--yes`` is added only to the commands that would
    otherwise prompt, because ``az`` rejects it on the ones that would not.
    """
    parts = [command]
    if resource_group:
        parts.append(f"--resource-group {arg(resource_group)}")
    parts.append(f"--subscription {arg(subscription)}")
    if _prompts(command):
        parts.append("--yes")
    return " ".join(parts)


def az_resource_delete(arm_id: str, subscription: str) -> str:
    """``az resource delete --ids``, for resource types core ``az`` has no verb for.

    Application Insights web tests are the case here: the dedicated command
    lives in an extension that is not installed by default, and a generated
    plan that needs an extension installed before it runs is a plan that does
    not run. ``az resource delete`` is part of core ``az`` and drives ARM
    directly, so it works for anything.
    """
    return f"az resource delete --ids {arg(arm_id)} --subscription {arg(subscription)}"


def arg(value: Any) -> str:
    """One shell argument, quoted if it needs it.

    Azure's own naming rules keep the characters that matter out of most
    resource names today, and resource *group* names allow more than most --
    parentheses and periods among them. Relying on a remote service's input
    validation for local shell safety is the assumption that ages badly: a
    name with a quote or a semicolon in it must stay one argument to the
    generated command, not become a second command.

    ``shlex.quote`` leaves an ordinary name untouched, so this costs nothing
    in the normal case and the generated plan stays readable.
    """
    return shlex.quote(str(value))


def location_of(resource: dict[str, Any]) -> str:
    """A resource's region, normalised, or ``global``."""
    return azure.region_of(resource.get("location") or "") or azure.GLOBAL
