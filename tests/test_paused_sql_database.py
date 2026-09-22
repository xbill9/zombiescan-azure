"""Paused SQL databases still paying for storage."""

from __future__ import annotations

from tests.conftest import load_fixture
from zombiescan.packs.core.paused_sql_database import paused_sql_database

FIXTURE = load_fixture("paused_sql_database")


def _findings(make_context):
    ctx, arm = make_context(
        {"Microsoft.Sql/servers": FIXTURE["servers"], "/databases": FIXTURE["databases"]}
    )
    return list(paused_sql_database(ctx)), arm


def test_only_paused_databases_are_reported(make_context):
    findings, _ = _findings(make_context)
    assert {f.resource_id for f in findings} == {"analytics-sql/reporting"}


def test_the_system_database_is_skipped(make_context):
    """Every server has a master; reporting one per server buries the rest."""
    findings, _ = _findings(make_context)
    assert not any(f.resource_id.endswith("/master") for f in findings)


def test_the_cost_is_provisioned_storage_at_the_service_tier_rate(make_context):
    findings, _ = _findings(make_context)
    database = findings[0]
    assert database.details["max_size_gb"] == 100
    assert database.monthly_cost == 100 * 0.115


def test_backup_storage_is_excluded_and_the_reason_is_given(make_context):
    """Backups are retained on their own schedule, so deleting does not free them."""
    findings, _ = _findings(make_context)
    assert "Backup storage is billed separately" in findings[0].details["note"]


def test_the_command_names_both_the_server_and_the_database(make_context):
    findings, _ = _findings(make_context)
    assert "--name reporting" in findings[0].remediation
    assert "--server analytics-sql" in findings[0].remediation
    assert "--yes" in findings[0].remediation
