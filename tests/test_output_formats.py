"""The JSON contract and the HTML report.

Both are things other people consume, so both need to be pinned: a consumer
that cannot tell which shape it is reading breaks silently, and a report that
renders as unstyled text in an email is worse than no report.
"""

from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

from zombiescan.engine import ScanError, ScanResult
from zombiescan.html import to_html
from zombiescan.models import Finding
from zombiescan.report import SCHEMA_VERSION, to_json

SCHEMA = json.loads(
    (pathlib.Path(__file__).parent.parent / "docs" / "findings.schema.json").read_text()
)


SUB = "aaaaaaaa-0000-0000-0000-000000000000"


def _finding(check="x", rid="r-1", cost=1.0, approx=False, details=None):
    return Finding(
        check=check,
        resource_id=rid,
        resource_type="thing",
        subscription=SUB,
        resource_group="prod-rg",
        arm_id=f"/subscriptions/{SUB}/resourceGroups/prod-rg/providers/Acme/things/{rid}",
        location="eastus",
        reason="because",
        monthly_cost=cost,
        remediation=f"az thing delete --name {rid}",
        approximate_cost=approx,
        details=details or {"note": "a caveat"},
    )


@pytest.fixture
def result():
    r = ScanResult(
        findings=[
            _finding("costly", "r-1", 50.0),
            _finding("costly", "r-2", 25.0),
            _finding("free", "r-3", 0.0),
        ],
        subscriptions=[SUB, "bbbbbbbb-0000-0000-0000-000000000000"],
        attempted=6,
    )
    r.errors = [ScanError(SUB, "costly", "AuthorizationFailed: no access")]
    return r


# --- JSON contract -------------------------------------------------------


def test_output_validates_against_the_published_schema(result):
    doc = to_json(result, principal="me@example.com", duration_seconds=1.5)
    jsonschema.validate(doc, SCHEMA)


def test_empty_scan_also_validates():
    jsonschema.validate(to_json(ScanResult(subscriptions=[SUB], attempted=1)), SCHEMA)


def test_schema_version_matches_the_published_schema():
    """If the code and the schema disagree about the version, consumers cannot trust either."""
    assert SCHEMA["properties"]["schema_version"]["const"] == SCHEMA_VERSION


def test_the_principal_is_recorded_so_a_clean_can_refuse_a_foreign_report(result):
    doc = to_json(result, principal="me@example.com")
    assert doc["scan"]["principal"] == "me@example.com"


def test_the_subscriptions_scanned_are_recorded(result):
    """A finding's subscription is meaningless without knowing what was looked at."""
    assert to_json(result)["scan"]["subscriptions"] == [
        SUB,
        "bbbbbbbb-0000-0000-0000-000000000000",
    ]


def test_a_finding_carries_the_resource_group_and_the_arm_id(result):
    """Neither is derivable from the rest, and nothing can be acted on without them."""
    finding = to_json(result)["findings"][0]
    assert finding["resource_group"] == "prod-rg"
    assert finding["arm_id"].startswith(f"/subscriptions/{SUB}/resourceGroups/prod-rg/")


def test_pairs_skipped_for_an_unregistered_provider_are_in_the_document(result):
    """A consumer has to be able to tell a clean subscription from one where
    most of the catalog never ran.

    Twelve of twenty-two skipped is a complete scan -- the other ten really
    did look and really did find nothing. What it is not is a clean bill of
    health for the whole catalog, which is why the count is published rather
    than folded into the completeness flag.
    """
    doc = to_json(ScanResult(subscriptions=[SUB], attempted=22, unavailable=12))
    assert doc["scan"]["pairs_unavailable"] == 12
    assert doc["scan"]["complete"] is True
    jsonschema.validate(doc, SCHEMA)


def test_a_subscription_where_no_provider_is_registered_is_not_complete(result):
    """Nothing could be read, so "no findings" says nothing at all."""
    doc = to_json(ScanResult(subscriptions=[SUB], attempted=22, unavailable=22))
    assert doc["scan"]["complete"] is False


def test_totals_are_grouped_by_check(result):
    totals = to_json(result)["totals"]
    assert totals["monthly_cost"] == pytest.approx(75.0)
    assert totals["annual_cost"] == pytest.approx(900.0)
    assert totals["by_check"]["costly"] == {"count": 2, "monthly_cost": 75.0}
    assert totals["by_check"]["free"] == {"count": 1, "monthly_cost": 0.0}


def test_price_table_date_is_recorded_for_auditability(result):
    doc = to_json(result, pricing_generated="2026-09-21T01:16:11Z")
    assert doc["pricing"]["generated"] == "2026-09-21T01:16:11Z"


def test_incomplete_scan_is_flagged_in_the_document(result):
    """A consumer must be able to tell "clean" from "could not look"."""
    assert to_json(result)["scan"]["complete"] is True
    dead = ScanResult(subscriptions=["x"], attempted=1)
    dead.errors = [ScanError("x", "c", "boom")]
    assert to_json(dead)["scan"]["complete"] is False


# --- HTML report ---------------------------------------------------------


def test_report_is_entirely_self_contained(result):
    """It gets emailed and opened offline. One external reference breaks it."""
    page = to_html(result)
    for marker in ("<link", "<script", "src=", "@import", "http://", "https://"):
        assert marker not in page, f"external reference: {marker}"


def test_hostile_resource_names_are_escaped():
    evil = "<script>alert(1)</script>"
    page = to_html(ScanResult(findings=[_finding(rid=evil)], subscriptions=[SUB], attempted=1))
    assert evil not in page
    assert "&lt;script&gt;" in page


def test_totals_and_findings_both_appear(result):
    page = to_html(result)
    assert "$75.00" in page and "$900.00" in page
    assert "r-1" in page and "r-3" in page


def test_negligible_total_is_worded_not_printed_as_zero():
    page = to_html(ScanResult(findings=[_finding(cost=0.0)], subscriptions=[SUB], attempted=1))
    assert "Under $0.01" in page
    assert "cleanup debt" in page


def test_a_failed_scan_carries_the_warning_banner():
    dead = ScanResult(subscriptions=["x"], attempted=1)
    dead.errors = [ScanError("x", "c", "boom")]
    page = to_html(dead)
    assert "Nothing could be scanned" in page
    assert "not an" in page and "all-clear" in page


def test_errors_are_shown_not_swallowed(result):
    assert "AuthorizationFailed" in to_html(result)


def test_print_stylesheet_exists():
    """Printing to PDF from a browser is how a PDF gets made; no rendering engine needed."""
    assert "@media print" in to_html(ScanResult(subscriptions=[SUB]))
