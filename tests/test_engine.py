"""Engine scheduling: which checks run where, and how failures are counted."""

from __future__ import annotations

import httplib2
import pytest
from googleapiclient.errors import HttpError

from tests.conftest import TEST_PRICES
from zombiescan.engine import ScanResult, filter_locations, scan, select_checks
from zombiescan.models import Finding
from zombiescan.pricing import PriceTable
from zombiescan.registry import CheckSpec

PROJECTS = ["proj-a", "proj-b", "proj-c"]


def _finding(check="c", project="proj-a", location="us-central1-a", cost=1.0):
    return Finding(
        check=check,
        resource_id=f"{check}-1",
        resource_type="t",
        project=project,
        location=location,
        reason="r",
        monthly_cost=cost,
        remediation="gcloud noop",
    )


def _spec(name, seen, raises=None):
    def fn(ctx):
        seen.append((name, ctx.project))
        if raises is not None:
            raise raises
        yield _finding(name, ctx.project)

    return CheckSpec(name=name, title=name, fn=fn)


def _http_error(status, message="boom"):
    response = httplib2.Response({"status": status})
    return HttpError(response, message.encode(), uri="https://example.googleapis.com/v1/x")


@pytest.fixture
def pricing():
    return PriceTable(TEST_PRICES)


def test_a_check_runs_once_per_project(pricing):
    """The unit of fan-out is the project: aggregatedList covers the locations."""
    seen = []
    result = scan(None, PROJECTS, [_spec("one", seen)], pricing)
    assert sorted(p for _, p in seen) == sorted(PROJECTS)
    assert result.attempted == 3
    assert len(result.findings) == 3
    assert result.completely_failed is False


def test_findings_are_sorted_costliest_first(pricing):
    result = ScanResult()
    result.findings = [_finding(cost=1.0), _finding(cost=50.0), _finding(cost=7.0)]
    result.findings.sort(key=lambda f: -f.monthly_cost)
    assert [f.monthly_cost for f in result.findings] == [50.0, 7.0, 1.0]


def test_a_disabled_api_is_not_an_error(pricing):
    """A project that never used Filestore has no Filestore waste."""
    disabled = _http_error(
        403, "Cloud Filestore API has not been used in project 1 before or it is disabled."
    )
    result = scan(None, ["proj-a"], [_spec("f", [], raises=disabled)], pricing)
    assert result.errors == []
    assert result.unavailable == 1
    # Nothing was scanned, so "no findings" is not an all-clear.
    assert result.completely_failed is True


def test_permission_denied_is_an_error_not_a_skip(pricing):
    """A 403 that is not a disabled API means the caller cannot see the resource."""
    result = scan(
        None, ["proj-a"], [_spec("f", [], raises=_http_error(403, "caller lacks IAM"))], pricing
    )
    assert result.unavailable == 0
    assert len(result.errors) == 1
    assert "permission denied" in result.errors[0].message


def test_one_failing_check_does_not_kill_the_scan(pricing):
    seen = []
    checks = [_spec("good", seen), _spec("bad", seen, raises=RuntimeError("nope"))]
    result = scan(None, ["proj-a"], checks, pricing)
    assert len(result.findings) == 1
    assert len(result.errors) == 1
    assert "RuntimeError: nope" in result.errors[0].message
    assert result.completely_failed is False


def test_a_scan_of_nothing_is_not_reported_as_a_failure(pricing):
    result = scan(None, [], [], pricing)
    assert result.attempted == 0
    assert result.completely_failed is False


# --------------------------------------------------------------------------
# location filtering


def test_naming_a_region_keeps_its_zones():
    """--location us-central1 must not exclude us-central1-a; that would be a trap."""
    findings = [
        _finding(location="us-central1-a"),
        _finding(location="us-central1"),
        _finding(location="europe-west1-b"),
    ]
    kept = filter_locations(findings, ("us-central1",))
    assert [f.location for f in kept] == ["us-central1-a", "us-central1"]


def test_global_findings_survive_any_location_filter():
    """A global resource belongs to no region, so no region filter can exclude it."""
    findings = [_finding(location="global"), _finding(location="europe-west1-b")]
    kept = filter_locations(findings, ("us-central1",))
    assert [f.location for f in kept] == ["global"]


def test_no_filter_keeps_everything():
    findings = [_finding(location="us-central1-a"), _finding(location="europe-west1-b")]
    assert filter_locations(findings, ()) == findings


# --------------------------------------------------------------------------
# check selection


def test_unknown_check_names_are_refused():
    with pytest.raises(ValueError, match="unknown check"):
        select_checks(("no-such-check",))


def test_asking_for_a_check_and_disabling_its_pack_is_refused():
    """Resolving the contradiction by guessing which the operator meant is worse."""
    with pytest.raises(ValueError, match="disabled by --disable-pack"):
        select_checks(("gke-idle-cluster",), frozenset({"gke"}))


def test_disabling_a_pack_drops_its_checks():
    selected = {spec.name for spec in select_checks((), frozenset({"gke"}))}
    assert "gke-idle-cluster" not in selected
    assert "unattached-disk" in selected
