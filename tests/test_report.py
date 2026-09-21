"""Report-layer behaviour that is easy to get quietly wrong."""

from __future__ import annotations

from rich.console import Console

from zombiescan.engine import ScanError, ScanResult
from zombiescan.models import Finding
from zombiescan.report import _representative, render, to_script


def _finding(check: str, resource_id: str, cost: float = 0.0) -> Finding:
    return Finding(
        check=check,
        resource_id=resource_id,
        resource_type="thing",
        project="proj-1",
        location="us-central1-a",
        reason="because",
        monthly_cost=cost,
        remediation=f"gcloud thing delete {resource_id}",
    )


def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (-f.monthly_cost, f.location, f.resource_id))


def test_every_check_survives_the_limit_when_costs_tie():
    """The real failure: 67 free log groups pushing 18 security groups out of sight."""
    findings = _sorted_findings(
        [_finding("log-group", f"lg-{i}") for i in range(67)]
        + [_finding("security-group", f"sg-{i}") for i in range(18)]
        + [_finding("empty-vpc", "vpc-1")]
    )
    picked = _representative(findings, limit=8)
    assert len(picked) == 8
    assert {f.check for f in picked} == {"log-group", "security-group", "empty-vpc"}


def test_cost_still_wins_when_costs_differ():
    findings = _sorted_findings(
        [_finding("pricey", "a", 100.0), _finding("cheap", "b", 1.0), _finding("cheap", "c", 0.5)]
    )
    picked = _representative(findings, limit=2)
    assert [f.resource_id for f in picked] == ["a", "b"]


def test_no_limit_returns_everything():
    findings = [_finding("x", str(i)) for i in range(5)]
    assert _representative(findings, limit=0) is findings
    assert _representative(findings, limit=99) is findings


def test_more_checks_than_slots_does_not_overflow():
    findings = _sorted_findings([_finding(f"check-{i}", f"r-{i}") for i in range(10)])
    assert len(_representative(findings, limit=3)) == 3


def test_negligible_total_is_not_reported_as_a_dollar_figure(capsys):
    """'$0.00/month ($0.01/year)' reads as a broken tool, not as cleanup debt."""
    result = ScanResult(findings=[_finding("x", "a")], projects=["proj-1"])
    render(result, Console(width=100, force_terminal=False))
    out = capsys.readouterr().out
    assert "under $0.01/month" in out
    assert "$0.01/year" not in out


def test_script_warns_before_the_first_command():
    result = ScanResult(findings=[_finding("x", "a", 5.0)], projects=["proj-1"])
    script = to_script(result)
    assert script.index("READ EVERY LINE") < script.index("gcloud thing delete")
    assert "did not run it" in script


def test_total_failure_is_not_reported_as_an_all_clear(capsys):
    """The worst possible bug: scanning nothing and calling the project clean."""
    result = ScanResult(findings=[], projects=["no-such-project"], errors=[], attempted=0)
    result.errors = [ScanError("no-such-project", "x", "permission denied: no access")]
    result.attempted = 1
    render(result, Console(width=100, force_terminal=False))
    out = capsys.readouterr().out
    assert "Nothing could be scanned" in out
    assert "not an all-clear" in out
    assert "No waste found" not in out


def test_partial_failure_still_reports_normally(capsys):
    result = ScanResult(findings=[], projects=["proj-1"], attempted=4)
    result.errors = [ScanError("proj-1", "x", "permission denied: no access")]
    render(result, Console(width=100, force_terminal=False))
    out = capsys.readouterr().out
    assert "No waste found" in out
    assert "Nothing could be scanned" not in out


def test_everything_filtered_out_is_not_no_waste(capsys):
    """'No waste found' after --min-cost hid 77 findings is a lie."""
    result = ScanResult(findings=[], projects=["proj-1"], attempted=4)
    render(result, Console(width=100, force_terminal=False), hidden_by_filter=77)
    out = capsys.readouterr().out
    assert "77 finding(s) were hidden by the cost filter" in out
    assert "No waste found" not in out


def test_script_header_does_not_promise_snapshots_for_everything(capsys):
    """Only some resources can be backed up first; the header must not claim all are."""
    result = ScanResult(findings=[_finding("security-group", "sg-1")], projects=["proj-1"])
    script = to_script(result)
    assert "Volumes are snapshotted first" not in script
    assert "Where a backup is possible" in script


HOSTILE_NAME = "/app/'; echo PWNED; #"

# The same trick without a path separator. Google addresses a secret by the
# last segment of its resource name, so a slash never reaches the command.
HOSTILE_ID = "leftover'; echo PWNED; #"


def test_remediation_cannot_break_out_of_shell_quoting():
    """A quote in a resource name must not become a command in the plan.

    Google's own naming rules make this unreachable through the bucket and
    secret APIs today. Relying on a remote service's input validation for
    local shell safety is the assumption that ages badly.
    """
    import shlex

    from tests.conftest import TEST_PRICES, FakeClient
    from zombiescan.models import ScanContext
    from zombiescan.packs.core.stale_secret import build_finding
    from zombiescan.pricing import PriceTable

    ctx = ScanContext(clients=None, project="proj-1", pricing=PriceTable(TEST_PRICES))
    ctx.client = lambda _api: FakeClient({})
    finding = build_finding(
        ctx,
        {"name": f"projects/proj-1/secrets/{HOSTILE_ID}", "replication": {"automatic": {}}},
        [{"state": "ENABLED", "createTime": "2020-01-01T00:00:00Z"}],
        400,
    )

    # The property that matters: a shell parsing this line sees the hostile
    # name as one argument to the delete, and sees no extra commands.
    tokens = shlex.split(finding.remediation)
    assert tokens[:3] == ["gcloud", "secrets", "delete"]
    assert tokens[3] == HOSTILE_ID
    assert "echo" not in tokens
    assert ";" not in tokens


def test_every_generated_command_names_its_project_and_never_prompts():
    """Two properties of every command a check emits, checked at the source.

    A command without --quiet blocks on a confirmation prompt, which the
    generated script cannot answer. One without --project acts on whatever
    project the operator's shell happens to be pointed at, which is the exact
    accident the report exists to prevent.
    """
    import pathlib as _pathlib
    import re

    import zombiescan.packs

    root = _pathlib.Path(zombiescan.packs.__file__).parent
    offenders = []
    for module in sorted(root.glob("*/*.py")):
        source = module.read_text()
        for match in re.finditer(r"remediation=\(?\s*((?:[^()]|\([^()]*\))*?)\),?\n", source):
            body = match.group(1)
            if "gcloud" not in body:
                continue
            if "helpers.gcloud(" in body:
                # helpers.gcloud appends both flags itself; test_shared covers it.
                continue
            if "--project=" not in body or "--quiet" not in body:
                offenders.append(f"{module.name}: {body.strip()[:70]}")
    assert offenders == [], "commands missing --project or --quiet:\n" + "\n".join(offenders)


def test_script_comments_cannot_become_commands():
    """A newline in a name would end the comment and execute what follows."""
    finding = Finding(
        check="x",
        resource_id="r\nrm -rf /",
        resource_type="t",
        project="proj-1",
        location="us-central1-a",
        reason="line one\nrm -rf /",
        monthly_cost=1.0,
        remediation="gcloud thing delete r",
    )
    script = to_script(ScanResult(findings=[finding], projects=["proj-1"]))
    for line in script.splitlines():
        assert "rm -rf /" not in line or line.startswith("#")
