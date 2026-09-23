#!/usr/bin/env python3
"""Regenerate the evidence for this article.

    python3 articles/zombiescan-azure/gather-evidence.py

Read-only against Azure: every command is a scan, a list, a get or a query --
Resource Graph, Azure Monitor metrics and Cost Management. `zombiescan clean`
runs without --apply. Each file carries a header naming the command and the
UTC time it ran; the output under it is verbatim.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
EVIDENCE = HERE / "evidence"
NOW = dt.datetime.now(dt.UTC)
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
ENV = {**os.environ, "COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb"}


def run(args: list[str], input_text: str | None = None) -> str:
    done = subprocess.run(
        args, cwd=REPO, env=ENV, capture_output=True, text=True, input=input_text, timeout=900
    )
    return (done.stdout + done.stderr).rstrip() + "\n"


def save(name: str, command: str, body: str) -> None:
    if name.endswith(".json"):
        # A JSON file stays JSON: the command and time ride along as fields.
        document = {"_command": command, "_captured": STAMP, "data": json.loads(body)}
        text = json.dumps(document, indent=2) + "\n"
    else:
        text = f"# {command}\n# captured {STAMP}\n\n{body}"
    (EVIDENCE / name).write_text(text)
    print(f"  {name}")


def az_json(args: list[str]) -> object:
    return json.loads(run(["az", *args, "-o", "json"]))


def graph(subscription: str, query: str) -> list[dict]:
    body = json.dumps(
        {
            "subscriptions": [subscription],
            "query": query,
            "options": {"resultFormat": "objectArray"},
        }
    )
    path = EVIDENCE / ".graph-body.json"
    path.write_text(body)
    try:
        out = az_json(
            [
                "rest",
                "--method",
                "post",
                "--url",
                "https://management.azure.com/providers/Microsoft.ResourceGraph/resources"
                "?api-version=2024-04-01",
                "--body",
                f"@{path}",
            ]
        )
    finally:
        path.unlink()
    return out["data"]


def cost(subscription: str, group: str, start: str, end: str, grouping: list[str]) -> dict:
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start, "to": end},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": g} for g in grouping],
        },
    }
    path = EVIDENCE / ".cost-body.json"
    path.write_text(json.dumps(body))
    try:
        out = az_json(
            [
                "rest",
                "--method",
                "post",
                "--url",
                f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/"
                f"{group}/providers/Microsoft.CostManagement/query?api-version=2023-03-01",
                "--body",
                f"@{path}",
            ]
        )
    finally:
        path.unlink()
    props = out["properties"]
    columns = [c["name"] for c in props["columns"]]
    return {
        "period": {"from": start, "to": end},
        "grouping": grouping,
        "rows": [dict(zip(columns, row)) for row in props["rows"]],
    }


def metric(resource: str, name: str, extra: str = "") -> dict:
    url = (
        f"https://management.azure.com{resource}/providers/Microsoft.Insights/metrics"
        f"?api-version=2024-02-01&metricnames={name}&timespan=P7D&aggregation=Total"
        f"&interval=FULL{extra}"
    )
    return az_json(["rest", "--method", "get", "--url", url])


def mcp_tools() -> str:
    frames = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "evidence", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    out = run(["uv", "run", "zombiescan-mcp"], "\n".join(json.dumps(f) for f in frames) + "\n")
    tools = []
    for line in out.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 2:
            tools = message["result"]["tools"]
    lines = [f"# {len(tools)} tool(s)", ""]
    lines += [f"  {t['name']}  {t['description'][:80]}" for t in tools]
    return "\n".join(lines) + "\n"


def main() -> int:
    EVIDENCE.mkdir(exist_ok=True)
    print(f"writing {EVIDENCE}")

    save(
        "build.txt",
        "uv sync && uv run zombiescan --version",
        run(["uv", "sync"]) + run(["uv", "run", "zombiescan", "--version"]),
    )
    save("checks.txt", "zombiescan checks", run(["uv", "run", "zombiescan", "checks"]))
    save("providers.txt", "zombiescan providers", run(["uv", "run", "zombiescan", "providers"]))

    save("scan-one.txt", "zombiescan scan", run(["uv", "run", "zombiescan", "scan"]))
    findings = EVIDENCE / "findings.json"
    report = EVIDENCE / "report.html"
    script = EVIDENCE / "cleanup.sh"
    save(
        "scan-all.txt",
        "zombiescan scan --all-subscriptions --json findings.json --html report.html --script cleanup.sh",
        run(
            [
                "uv",
                "run",
                "zombiescan",
                "scan",
                "--all-subscriptions",
                "--json",
                str(findings),
                "--html",
                str(report),
                "--script",
                str(script),
            ]
        ),
    )
    save(
        "clean-dryrun.txt",
        "zombiescan clean --all-subscriptions",
        run(["uv", "run", "zombiescan", "clean", "--all-subscriptions"]),
    )
    save(
        "fixture-findings.txt",
        "python3 articles/zombiescan-azure/show-fixture-findings.py",
        run(["uv", "run", "python", str(HERE / "show-fixture-findings.py")]),
    )
    save("pytest.txt", "uv run pytest -q", run(["uv", "run", "pytest", "-q"]))
    save("mcp-tools.txt", "zombiescan-mcp, tools/list over stdio", mcp_tools())

    role = json.loads((REPO / "policy/zombiescan-scanner-role.json").read_text())
    actions = [a for p in role["permissions"] for a in p["actions"]]
    save(
        "role.txt",
        "policy/zombiescan-scanner-role.json, actions",
        f"{len(actions)} action(s), {sum(a.endswith('/read') for a in actions)} ending /read\n\n"
        + "\n".join(actions)
        + "\n",
    )

    accounts = az_json(["account", "list", "--all"])
    save(
        "accounts.json",
        "az account list --all (ids and tenant fields only)",
        json.dumps(
            [
                {
                    k: a.get(k)
                    for k in (
                        "name",
                        "id",
                        "tenantId",
                        "tenantDisplayName",
                        "tenantDefaultDomain",
                        "isDefault",
                        "state",
                    )
                }
                for a in accounts
            ],
            indent=2,
        )
        + "\n",
    )
    subscription = next(a["id"] for a in accounts if a.get("isDefault"))

    save(
        "inventory.json",
        "Resource Graph: Resources | summarize count() by type, location, resourceGroup",
        json.dumps(
            graph(
                subscription,
                "Resources | summarize count=count() by type, location, "
                "resourceGroup | order by type asc",
            ),
            indent=2,
        )
        + "\n",
    )

    group = "research-mesh-rg"
    rg = f"/subscriptions/{subscription}/resourceGroups/{group}/providers"
    today = NOW.strftime("%Y-%m-%dT23:59:59Z")
    month_ago = (NOW - dt.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
    save(
        "cost-management.json",
        f"Cost Management query, resource group {group}",
        json.dumps(
            {
                "last_30_days_total": cost(subscription, group, month_ago, today, []),
                "last_30_days_by_resource": cost(
                    subscription, group, month_ago, today, ["ResourceId", "MeterCategory"]
                ),
                "since_2026_08_13_by_meter": cost(
                    subscription, group, "2026-08-13T00:00:00Z", today, ["MeterCategory", "Meter"]
                ),
            },
            indent=2,
        )
        + "\n",
    )
    save(
        "metrics.json",
        "Azure Monitor metrics, 7 days, research-mesh-rg",
        json.dumps(
            {
                "container_app_requests": metric(
                    f"{rg}/Microsoft.App/containerApps/research-azure", "Requests"
                ),
                "foundry_model_requests_by_deployment": metric(
                    f"{rg}/Microsoft.CognitiveServices/accounts/research-mesh-foundry",
                    "ModelRequests",
                    "&$filter=ModelDeploymentName%20eq%20'*'",
                ),
            },
            indent=2,
        )
        + "\n",
    )
    registered = az_json(
        ["provider", "show", "-n", "Microsoft.Insights", "--query", "registrationState"]
    )
    save(
        "insights-registration.txt",
        "az provider show -n Microsoft.Insights --query registrationState",
        f"{registered}\n",
    )

    save(
        "derived-figures.txt",
        "python3 articles/zombiescan-azure/derive-figures.py",
        run(["uv", "run", "python", str(HERE / "derive-figures.py")]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
