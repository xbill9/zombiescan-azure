"""Output: terminal table, JSON, and the cleanup script.

The cleanup script is *written*, never executed. zombiescan has no code path
that runs it.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from rich.console import Console
from rich.table import Table

from zombiescan import __version__, packs
from zombiescan.engine import ScanResult


def _money(amount: float, approximate: bool = False) -> str:
    return f"{'~' if approximate else ''}${amount:,.2f}"


DEFAULT_LIMIT = 25


def _summary_table(result: ScanResult) -> Table:
    """One row per check: how many, and what they cost together.

    With dozens of cheap findings the per-resource table stops being readable,
    and this is what people actually act on.
    """
    counts: dict[str, int] = {}
    totals: dict[str, float] = {}
    for finding in result.findings:
        counts[finding.check] = counts.get(finding.check, 0) + 1
        totals[finding.check] = totals.get(finding.check, 0.0) + finding.monthly_cost

    table = Table(header_style="bold", box=None, pad_edge=False)
    table.add_column("Check", no_wrap=True)
    table.add_column("Found", justify="right", no_wrap=True)
    table.add_column("Monthly", justify="right", no_wrap=True)
    for name in sorted(counts, key=lambda n: (-totals[n], n)):
        table.add_row(name, str(counts[name]), _money(totals[name]))
    return table


def _representative(findings: list[Any], limit: int) -> list[Any]:
    """The costliest findings, but never hiding a check entirely.

    Sorting purely by cost means a tie at zero -- which is the normal case for
    hygiene findings -- lets whichever check has the most rows fill the table
    and push every other check out of sight. Each check gets its costliest row
    first, then the remaining slots go to the next costliest overall.
    """
    if limit <= 0 or limit >= len(findings):
        return findings

    best_per_check: dict[str, Any] = {}
    for finding in findings:  # already sorted by cost, descending
        best_per_check.setdefault(finding.check, finding)

    picked = list(best_per_check.values())[:limit]
    chosen = {id(f) for f in picked}
    for finding in findings:
        if len(picked) >= limit:
            break
        if id(finding) not in chosen:
            picked.append(finding)
            chosen.add(id(finding))

    picked.sort(key=lambda f: (-f.monthly_cost, f.check, f.location, f.resource_id))
    return picked


def _detail_table(findings: list[Any], width: int, multi_subscription: bool) -> Table:
    """Per-resource rows, laid out for the terminal actually in use.

    Resource identifiers get long -- a subnet is reported as
    "network/subnet" and an ARM path runs past 200 characters. Pinning a wide
    Resource column crushes everything else, so the reason column is dropped
    on narrow terminals instead of being squeezed to six characters. The
    check name in the summary already says what kind of finding it is, and
    --json always carries the full reason and the full ARM id.

    The subscription column appears only when the scan covered more than one,
    since a single-subscription scan repeats the same value down the page.
    The resource group is always shown: it is what an operator needs to act
    on a finding, and it is not derivable from anything else in the row.
    """
    wide = width >= 100
    table = Table(header_style="bold", expand=True)
    if multi_subscription:
        table.add_column("Subscription", no_wrap=True, overflow="ellipsis", ratio=2)
    table.add_column("Location", no_wrap=True, min_width=11)
    table.add_column("Resource group", no_wrap=True, overflow="ellipsis", ratio=2)
    table.add_column("Resource", no_wrap=True, overflow="ellipsis", ratio=3)
    table.add_column("Monthly", justify="right", no_wrap=True, min_width=8)
    if wide:
        table.add_column("Why", ratio=4)

    for finding in findings:
        row = [finding.subscription] if multi_subscription else []
        row += [
            finding.location,
            finding.resource_group,
            finding.resource_id,
            _money(finding.monthly_cost, finding.approximate_cost),
        ]
        if wide:
            row.append(finding.reason)
        table.add_row(*row)
    return table


def render(
    result: ScanResult,
    console: Console,
    limit: int = DEFAULT_LIMIT,
    hidden_by_filter: int = 0,
) -> None:
    word = "subscription" if len(result.subscriptions) == 1 else "subscriptions"
    scope = f"{len(result.subscriptions)} {word}"

    if not result.findings:
        if result.completely_failed:
            # Reporting a clean account when nothing could be scanned is the
            # worst possible outcome: a false all-clear.
            console.print(
                f"\n[red]Nothing could be scanned[/red] — all {result.attempted} "
                f"subscription/check pairs failed. The result below is not an all-clear.\n"
            )
        elif hidden_by_filter:
            console.print(
                f"\n[yellow]All {hidden_by_filter} finding(s) were hidden by the cost "
                f"filter.[/yellow] Lower --min-cost to see them.\n"
            )
        else:
            console.print(f"\n[green]No waste found[/green] across {scope}. Nothing to clean up.\n")
        _render_unavailable(result, console)
        _render_errors(result, console)
        return

    count = len(result.findings)
    console.print(
        f"\n[bold]zombiescan — {count} finding{'' if count == 1 else 's'} across {scope}[/bold]\n"
    )
    console.print(_summary_table(result))

    picked = _representative(result.findings, limit)
    console.print()
    console.print(_detail_table(picked, console.width, len(result.subscriptions) > 1))
    if len(picked) < count:
        console.print(
            f"[dim]showing {len(picked)} of {count}, costliest first with every check "
            f"represented; use --limit 0 for all, or --json for the full set[/dim]"
        )

    total = result.total_monthly_cost
    if total >= 0.01:
        console.print(
            f"\n[bold]Estimated waste: {_money(total)}/month ({_money(total * 12)}/year)[/bold]"
        )
    else:
        # Saying "$0.00/month ($0.01/year)" makes the tool look broken. These
        # findings are real debt -- unbounded growth, blocked deletions -- they
        # just are not on this month's bill.
        console.print(
            "\n[bold]Estimated waste: under $0.01/month.[/bold] "
            "These cost almost nothing today; they are cleanup debt, not a bill."
        )

    if any(f.approximate_cost for f in result.findings):
        console.print(
            "[dim]~ marks an estimate or upper bound; the per-finding 'note' in "
            "--json output says why.[/dim]"
        )
    console.print(
        "[dim]Estimates from list prices, not your bill. zombiescan is read-only "
        "and deleted nothing.[/dim]"
    )
    _render_unavailable(result, console)
    _render_errors(result, console)


def _render_unavailable(result: ScanResult, console: Console) -> None:
    """Say how many checks never ran because their provider is not registered.

    Without this, a scan where two thirds of the catalog could not run reads
    as a clean bill of health. The pairs are not errors -- a subscription that
    has never used App Service has no App Service waste -- but "no waste
    found" and "no waste found in the ten services you actually use" are
    different statements, and only one of them is what happened.
    """
    if not result.unavailable:
        return
    console.print(
        f"\n[dim]{result.unavailable} of {result.attempted} subscription/check pair(s) "
        f"were skipped: their resource provider is not registered, so there is nothing "
        f"of that kind here. 'zombiescan providers' lists what each check reads.[/dim]"
    )


def _render_errors(result: ScanResult, console: Console) -> None:
    if not result.errors:
        return
    console.print(
        f"\n[yellow]{len(result.errors)} subscription/check pair(s) could not be scanned:[/yellow]"
    )
    for error in result.errors[:10]:
        console.print(f"  [dim]{error.subscription} {error.check}: {error.message}[/dim]")
    if len(result.errors) > 10:
        console.print(f"  [dim]... and {len(result.errors) - 10} more[/dim]")


SCHEMA_VERSION = 4


def _by_check(result: ScanResult) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for finding in result.findings:
        row = out.setdefault(finding.check, {"count": 0, "monthly_cost": 0.0})
        row["count"] += 1
        row["monthly_cost"] += finding.monthly_cost
    for row in out.values():
        row["monthly_cost"] = round(row["monthly_cost"], 2)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["monthly_cost"], kv[0])))


def to_json(
    result: ScanResult,
    principal: str | None = None,
    duration_seconds: float | None = None,
    pricing_generated: str | None = None,
) -> dict[str, Any]:
    """The machine-readable report.

    ``schema_version`` is the contract. Anything consuming this should check
    it: the shape will change, and a consumer that cannot tell which version
    it is reading breaks silently rather than loudly.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "zombiescan", "version": __version__},
        # Which packs were loaded, and at what version. A finding means nothing
        # without knowing which check produced it, and a check means nothing
        # without knowing which pack -- two packs may both ship a check called
        # "idle-cluster" and disagree about what idle means.
        "packs": packs.manifest(),
        "scan": {
            "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration_seconds, 2) if duration_seconds else None,
            "principal": principal,
            "subscriptions": result.subscriptions,
            "pairs_attempted": result.attempted,
            # Pairs where the resource provider was simply not registered on
            # that subscription. Not failures: a subscription that has never
            # used App Service has no App Service waste.
            "pairs_unavailable": result.unavailable,
            "complete": not result.completely_failed,
        },
        "pricing": {
            # Which price table produced these numbers. Without it a report is
            # not auditable: nobody can tell whether it used current rates.
            "generated": pricing_generated,
            "basis": "Azure Retail Prices API, pay-as-you-go USD list prices",
            "excludes": [
                "reservations",
                "savings plans",
                "Azure Hybrid Benefit",
                "dev/test rates",
                "enterprise agreement pricing",
                "credits",
            ],
        },
        "totals": {
            "monthly_cost": round(result.total_monthly_cost, 2),
            "annual_cost": round(result.total_monthly_cost * 12, 2),
            "finding_count": len(result.findings),
            "by_check": _by_check(result),
        },
        "findings": [f.to_dict() for f in result.findings],
        "errors": [
            {"subscription": e.subscription, "check": e.check, "message": e.message}
            for e in result.errors
        ],
    }


def write_json(result: ScanResult, path: str, **kwargs: Any) -> None:
    with open(path, "w") as handle:
        json.dump(to_json(result, **kwargs), handle, indent=2)
        handle.write("\n")


def _comment(text: str) -> str:
    """Flatten text destined for a shell comment onto one line.

    A newline inside a resource name or reason would end the comment and turn
    whatever follows into an executable line.
    """
    return " ".join(str(text).splitlines())


def to_script(result: ScanResult) -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "#!/usr/bin/env bash",
        f"# zombiescan cleanup plan — generated {stamp}",
        "#",
        "# READ EVERY LINE BEFORE RUNNING THIS.",
        "# zombiescan generated this file and did not run it. Some of these",
        "# deletions can be undone and some cannot: Key Vault and SQL keep a",
        "# recovery window, a released public IP address is gone for good.",
        "# Where a backup is possible the command takes one first, but a",
        "# backup is not a substitute for knowing what you are deleting.",
        "#",
        f"# {len(result.findings)} resource(s), about ${result.total_monthly_cost:,.2f}/month.",
        "",
        "set -euo pipefail",
        "",
    ]
    for finding in result.findings:
        lines.append(
            f"# {_comment(finding.subscription)} {_comment(finding.resource_group)} "
            f"{_comment(finding.location)} {_comment(finding.resource_id)} "
            f"— {_comment(finding.reason)}"
        )
        lines.append(f"# saves about ${finding.monthly_cost:,.2f}/month")
        lines.append(finding.remediation)
        lines.append("")
    return "\n".join(lines)


def write_script(result: ScanResult, path: str) -> None:
    with open(path, "w") as handle:
        handle.write(to_script(result))
