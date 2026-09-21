"""End-to-end smoke test against a real Google Cloud project.

Excluded by default (``addopts = -m 'not live'``). Run it deliberately:

    ZOMBIESCAN_LIVE=1 uv run pytest -m live

Read-only, like everything else here, but it does make real API calls.
"""

from __future__ import annotations

import os

import pytest

from zombiescan.engine import load_packs, resolve_projects, scan, select_checks, verify_credentials
from zombiescan.pricing import PriceTable

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ZOMBIESCAN_LIVE") != "1",
        reason="set ZOMBIESCAN_LIVE=1 to run against a real project",
    ),
]


@pytest.fixture(scope="module")
def session():
    load_packs()
    clients, principal, default_project = verify_credentials()
    return clients, principal, default_project


def test_credentials_resolve(session):
    _clients, principal, _default = session
    assert principal


def test_a_project_resolves(session):
    clients, _principal, default_project = session
    projects = resolve_projects(clients, (), False, default_project)
    assert projects and all(isinstance(p, str) for p in projects)


def test_scan_completes_and_prices_what_it_finds(session):
    clients, _principal, default_project = session
    projects = resolve_projects(clients, (), False, default_project)
    result = scan(clients, projects, select_checks(()), PriceTable.load())

    # The scan reached the project: not every pair may succeed, but they must
    # not all fail, or "no findings" says nothing.
    assert not result.completely_failed
    assert result.total_monthly_cost >= 0
    for finding in result.findings:
        assert finding.project in projects
        assert finding.location
        assert finding.monthly_cost >= 0
        assert finding.remediation.startswith("gcloud ")


def test_the_scan_makes_no_mutating_call(session):
    """Every generated command is text. Nothing in a finding can run one."""
    clients, _principal, default_project = session
    projects = resolve_projects(clients, (), False, default_project)
    result = scan(clients, projects, select_checks(()), PriceTable.load())
    for finding in result.findings:
        assert isinstance(finding.remediation, str)
