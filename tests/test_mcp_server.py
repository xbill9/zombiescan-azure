"""The MCP server: protocol, tool behaviour, and the read-only guarantee.

Offline. The tools that need Google Cloud (`scan_project`, `plan_cleanup`) are exercised
through the pieces that do not -- the summariser, the filter, the report
loader -- because the value of these tools is in the numbers they compute, and
those are all computed from a report document.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
from typing import Any

import pytest

from zombiescan import mcp_server, report

REPORT: dict[str, Any] = {
    "schema_version": report.SCHEMA_VERSION,
    "tool": {"name": "zombiescan", "version": "0.1.0"},
    "packs": [],
    "scan": {
        "generated": "2026-09-21T10:00:00Z",
        "duration_seconds": 4.2,
        "principal": "me@example.com",
        "projects": ["proj-1", "proj-2"],
        "pairs_attempted": 40,
        "pairs_unavailable": 0,
        "complete": True,
    },
    "pricing": {
        "generated": "2026-09-20",
        "basis": "Google Cloud Billing Catalog API, on-demand USD list prices",
    },
    "totals": {
        "monthly_cost": 76.30,
        "annual_cost": 915.60,
        "finding_count": 4,
        "by_check": {
            "gke-idle-cluster": {"count": 1, "monthly_cost": 32.85},
            "unattached-disk": {"count": 2, "monthly_cost": 39.80},
            "unused-static-ip": {"count": 1, "monthly_cost": 3.65},
        },
    },
    "findings": [
        {
            "check": "unattached-disk",
            "resource_id": "data-aaa",
            "resource_type": "compute-disk",
            "project": "proj-1",
            "location": "us-central1-a",
            "reason": "400 GB pd-balanced disk attached to no instance",
            "monthly_cost": 32.00,
            "approximate_cost": False,
            "remediation": "gcloud compute disks delete data-aaa --zone=us-central1-a",
            "details": {"size_gb": 400},
        },
        {
            "check": "gke-idle-cluster",
            "resource_id": "cluster-bbb",
            "resource_type": "gke-cluster",
            "project": "proj-2",
            "location": "europe-west1",
            "reason": "GKE cluster runs no nodes but still pays the management fee",
            "monthly_cost": 32.85,
            "approximate_cost": True,
            "remediation": "gcloud container clusters delete cluster-bbb --location=europe-west1",
            "details": {},
        },
        {
            "check": "unattached-disk",
            "resource_id": "data-ccc",
            "resource_type": "compute-disk",
            "project": "proj-2",
            "location": "europe-west1-b",
            "reason": "100 GB pd-balanced disk attached to no instance",
            "monthly_cost": 7.80,
            "approximate_cost": False,
            "remediation": "gcloud compute disks delete data-ccc --zone=europe-west1-b",
            "details": {"size_gb": 100},
        },
        {
            "check": "unused-static-ip",
            "resource_id": "ip-ddd",
            "resource_type": "compute-address",
            "project": "proj-1",
            "location": "us-central1",
            "reason": "Static IP reserved but attached to nothing",
            "monthly_cost": 3.65,
            "approximate_cost": False,
            "remediation": "gcloud compute addresses delete ip-ddd --region=us-central1",
            "details": {},
        },
    ],
    "errors": [],
}


@pytest.fixture
def report_path(tmp_path: pathlib.Path) -> str:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(REPORT))
    return str(path)


def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool the way a client does, and unwrap the JSON it returns."""
    result = mcp_server.call_tool(name, arguments)
    payload = json.loads(result["content"][0]["text"])
    payload["_is_error"] = result["isError"]
    return payload


# -- protocol ---------------------------------------------------------------


def test_initialize_reports_the_server_and_its_tools() -> None:
    response = mcp_server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    assert response is not None
    result = response["result"]
    # The client asked for a version we know, so we answer in it rather than
    # forcing our own and making the client negotiate again.
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "zombiescan"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert "read-only" in result["instructions"]


def test_initialize_falls_back_to_our_version_for_an_unknown_one() -> None:
    response = mcp_server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        }
    )
    assert response["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_tools_list_advertises_every_tool_with_a_schema() -> None:
    response = mcp_server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = response["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "scan_project",
        "estimate_savings",
        "explain_finding",
        "list_checks",
        "plan_cleanup",
    }
    for entry in tools:
        assert entry["description"]
        assert entry["inputSchema"]["type"] == "object"


def test_a_notification_gets_no_response() -> None:
    assert mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unknown_method_is_a_protocol_error() -> None:
    response = mcp_server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert response["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_an_unknown_tool_is_a_tool_error_not_a_protocol_error() -> None:
    payload = call("delete_everything", {})
    assert payload["_is_error"] is True
    assert "No tool named" in payload["error"]


def test_serve_round_trips_line_delimited_json(report_path: str) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "estimate_savings", "arguments": {"report_path": report_path}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout)

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    # Four requests and one notification, which is answered with silence.
    assert [r["id"] for r in responses] == [1, 2, 3, 4]
    savings = json.loads(responses[2]["result"]["content"][0]["text"])
    assert savings["matched"]["monthly_cost"] == 76.30


def test_a_malformed_line_does_not_stop_the_server() -> None:
    stdin = io.StringIO('not json\n{"jsonrpc": "2.0", "id": 9, "method": "ping"}\n')
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == mcp_server.PARSE_ERROR
    assert responses[1]["id"] == 9


# -- estimate_savings: the arithmetic ---------------------------------------


def test_estimate_savings_totals_the_whole_report(report_path: str) -> None:
    payload = call("estimate_savings", {"report_path": report_path})
    assert payload["matched"]["count"] == 4
    assert payload["matched"]["monthly_cost"] == 76.30
    assert payload["matched"]["annual_cost"] == 915.60
    assert payload["matched"]["costliest"]["resource_id"] == "cluster-bbb"
    assert payload["matched"]["cheapest"]["resource_id"] == "ip-ddd"
    assert payload["matched"]["approximate_count"] == 1


def test_estimate_savings_groups_by_check_region_and_type(report_path: str) -> None:
    payload = call("estimate_savings", {"report_path": report_path})
    assert payload["by_check"][0] == {
        "check": "unattached-disk",
        "count": 2,
        "monthly_cost": 39.80,
    }
    assert payload["by_location"] == [
        {"location": "europe-west1", "count": 1, "monthly_cost": 32.85},
        {"location": "us-central1-a", "count": 1, "monthly_cost": 32.0},
        {"location": "europe-west1-b", "count": 1, "monthly_cost": 7.8},
        {"location": "us-central1", "count": 1, "monthly_cost": 3.65},
    ]
    assert payload["by_project"] == [
        {"project": "proj-2", "count": 2, "monthly_cost": 40.65},
        {"project": "proj-1", "count": 2, "monthly_cost": 35.65},
    ]
    assert {row["resource_type"] for row in payload["by_resource_type"]} == {
        "compute-disk",
        "gke-cluster",
        "compute-address",
    }


def test_estimate_savings_filters_and_reports_what_it_filtered_on(report_path: str) -> None:
    payload = call(
        "estimate_savings",
        {"report_path": report_path, "checks": ["unattached-disk"], "locations": ["us-central1-a"]},
    )
    assert payload["matched"]["count"] == 1
    assert payload["matched"]["monthly_cost"] == 32.00
    assert payload["not_matched"] == {"count": 3, "monthly_cost": 44.30}
    assert payload["filter_applied"]["checks"] == ["unattached-disk"]
    assert payload["filter_applied"]["findings_matched"] == 1


def test_a_cost_filter_is_inclusive_at_both_ends(report_path: str) -> None:
    payload = call(
        "estimate_savings", {"report_path": report_path, "min_cost": 7.80, "max_cost": 32.00}
    )
    assert sorted(f["resource_id"] for f in payload["matched_findings"]) == ["data-aaa", "data-ccc"]


def test_a_filter_matching_nothing_says_which_term_was_wrong(report_path: str) -> None:
    """An exact zero from a mistyped filter reads like good news otherwise."""
    payload = call(
        "estimate_savings", {"report_path": report_path, "checks": ["unattached-persistent-disks"]}
    )
    assert payload["matched"]["count"] == 0
    assert payload["filter_applied"]["no_such_checks_in_report"] == ["unattached-persistent-disks"]


def test_a_single_string_is_accepted_where_a_list_is_asked_for(report_path: str) -> None:
    payload = call("estimate_savings", {"report_path": report_path, "checks": "unattached-disk"})
    assert payload["matched"]["count"] == 2


def test_a_missing_report_names_the_fix(report_path: str) -> None:
    payload = call("estimate_savings", {"report_path": "/nonexistent/report.json"})
    assert payload["_is_error"] is True
    assert "scan_project" in payload["error"]


def test_a_report_from_another_schema_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "old.json"
    path.write_text(json.dumps({**REPORT, "schema_version": 1}))
    payload = call("estimate_savings", {"report_path": str(path)})
    assert payload["_is_error"] is True
    assert "schema_version 1" in payload["error"]


# -- explain_finding --------------------------------------------------------


def test_explain_finding_describes_a_check_from_its_module() -> None:
    payload = call("explain_finding", {"check": "unattached-disk"})
    assert payload["title"] == "Unattached Persistent Disks"
    assert payload["pack"] == "core"
    assert "attached to no instance" in payload["why_it_is_waste"]
    assert payload["cleanable"] is True


def test_explain_finding_names_the_apis_a_check_calls() -> None:
    """An operator whose scan reported nothing needs to know which API to enable."""
    payload = call("explain_finding", {"check": "gke-idle-cluster"})
    assert payload["apis"] == ["container"]
    assert payload["pack"] == "gke"


def test_explain_finding_reports_a_refusal_to_clean() -> None:
    from zombiescan.registry import CHECKS

    refusing = next(name for name, spec in CHECKS.items() if spec.uncleanable)
    payload = call("explain_finding", {"check": refusing})
    assert payload["cleanable"] is False
    assert payload["refuses_to_clean_because"] == CHECKS[refusing].uncleanable


def test_explain_finding_looks_a_resource_up_in_a_report(report_path: str) -> None:
    payload = call("explain_finding", {"resource_id": "cluster-bbb", "report_path": report_path})
    assert payload["check"] == "gke-idle-cluster"
    assert payload["finding"]["monthly_cost"] == 32.85
    assert "Approximate" in payload["cost_basis"]
    assert "never run" in payload["remediation_note"]


def test_explain_finding_needs_a_report_to_look_a_resource_up() -> None:
    payload = call("explain_finding", {"resource_id": "data-aaa"})
    assert payload["_is_error"] is True
    assert "report_path" in payload["error"]


def test_explain_finding_on_an_unscanned_resource_says_so(report_path: str) -> None:
    payload = call("explain_finding", {"resource_id": "vol-zzz", "report_path": report_path})
    assert payload["_is_error"] is True
    assert "did not flag it" in payload["error"]


def test_explain_finding_on_an_unknown_check_lists_the_real_ones() -> None:
    payload = call("explain_finding", {"check": "unattached-elephants"})
    assert payload["_is_error"] is True
    assert "unattached-disk" in payload["error"]


# -- list_checks ------------------------------------------------------------


def test_list_checks_covers_every_registered_check() -> None:
    from zombiescan.registry import CHECKS

    payload = call("list_checks", {})
    assert payload["count"] == len(CHECKS)
    assert {row["check"] for row in payload["checks"]} == set(CHECKS)


def test_list_checks_filters_by_pack() -> None:
    payload = call("list_checks", {"pack": "gke"})
    assert payload["count"] > 0
    assert {row["pack"] for row in payload["checks"]} == {"gke"}


def test_list_checks_can_show_only_what_can_be_cleaned() -> None:
    payload = call("list_checks", {"cleanable_only": True})
    assert all(row["cleanable"] for row in payload["checks"])


# -- summarise --------------------------------------------------------------


def test_summarise_returns_computed_totals_and_the_costliest_rows() -> None:
    summary = mcp_server.summarise(REPORT, limit=2)
    assert summary["totals"] == {
        "finding_count": 4,
        "monthly_cost": 76.30,
        "annual_cost": 915.60,
    }
    assert len(summary["top_findings"]) == 2
    assert summary["by_check"][0]["check"] == "gke-idle-cluster"
    assert summary["principal"] == "me@example.com"
    assert summary["projects_scanned"] == ["proj-1", "proj-2"]
    assert "warning" not in summary


def test_summarise_warns_that_a_failed_scan_is_not_an_all_clear() -> None:
    document = {
        **REPORT,
        "findings": [],
        "totals": {"monthly_cost": 0.0, "annual_cost": 0.0, "finding_count": 0, "by_check": {}},
        "scan": {**REPORT["scan"], "complete": False, "pairs_attempted": 54},
    }
    summary = mcp_server.summarise(document)
    assert "not an all-clear" in summary["warning"]
    assert "54" in summary["warning"]


# -- the read-only guarantee ------------------------------------------------


def test_the_server_cannot_apply_a_cleanup() -> None:
    """The one thing that executes a plan must not be reachable from here.

    `plan_cleanup` calls `clean.plan_for`, which only ever builds Step objects.
    `clean.apply_outcome` is what sends them to AWS; this module may not name
    it, or anything else from `clean` beyond planning.
    """
    source = pathlib.Path(mcp_server.__file__).read_text()
    assert "apply_outcome" not in source
    assert set(re.findall(r"\bclean\.(\w+)", source)) == {"plan_for", "UNSUPPORTED"}


def test_no_tool_offers_to_change_anything() -> None:
    names = {entry["name"] for entry in mcp_server.TOOLS}
    assert names == set(mcp_server.HANDLERS)
    assert not {n for n in names if n.split("_")[0] in ("delete", "apply", "remove", "release")}
