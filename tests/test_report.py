"""Report-layer behaviour that is easy to get quietly wrong."""

from __future__ import annotations

import pathlib
import re
import shlex

from rich.console import Console

from tests.conftest import SUBSCRIPTION
from zombiescan.engine import ScanError, ScanResult
from zombiescan.models import Finding
from zombiescan.report import _representative, render, to_script


def _finding(check: str, resource_id: str, cost: float = 0.0) -> Finding:
    return Finding(
        check=check,
        resource_id=resource_id,
        resource_type="thing",
        subscription=SUBSCRIPTION,
        resource_group="test-rg",
        location="eastus",
        reason="because",
        monthly_cost=cost,
        remediation=f"az thing delete --name {resource_id}",
    )


def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (-f.monthly_cost, f.location, f.resource_id))


def test_every_check_survives_the_limit_when_costs_tie():
    """The real failure: 67 free NICs pushing 18 security groups out of sight."""
    findings = _sorted_findings(
        [_finding("orphaned-nic", f"nic-{i}") for i in range(67)]
        + [_finding("unused-nsg", f"nsg-{i}") for i in range(18)]
        + [_finding("empty-vnet", "vnet-1")]
    )
    picked = _representative(findings, limit=8)
    assert len(picked) == 8
    assert {f.check for f in picked} == {"orphaned-nic", "unused-nsg", "empty-vnet"}


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


def test_negligible_total_is_not_reported_as_a_dollar_figure(capsys):
    """'$0.00/month ($0.01/year)' reads as a broken tool, not as cleanup debt."""
    result = ScanResult(findings=[_finding("x", "a")], subscriptions=["sub-1"])
    render(result, Console(width=100, force_terminal=False))
    out = capsys.readouterr().out
    assert "under $0.01/month" in out
    assert "$0.01/year" not in out


def test_total_failure_is_not_reported_as_an_all_clear(capsys):
    """The worst possible bug: scanning nothing and calling the subscription clean."""
    result = ScanResult(findings=[], subscriptions=["no-such-sub"], attempted=1)
    result.errors = [ScanError("no-such-sub", "x", "permission denied: no access")]
    render(result, Console(width=100, force_terminal=False))
    # Rich wraps to the terminal width, so the assertion reads the text
    # rather than the line breaks.
    out = " ".join(capsys.readouterr().out.split())
    assert "Nothing could be scanned" in out
    assert "not an all-clear" in out
    assert "No waste found" not in out


def test_checks_skipped_for_an_unregistered_provider_are_counted_out_loud(capsys):
    """ "No waste found" and "no waste in the ten services you use" differ.

    ARM answers an unregistered provider with an empty page, so most of the
    catalog can be silently inapplicable. Saying so is what keeps a partial
    sweep from reading as a full one.
    """
    result = ScanResult(findings=[], subscriptions=["sub-1"], attempted=22, unavailable=12)
    render(result, Console(width=100, force_terminal=False))
    out = " ".join(capsys.readouterr().out.split())
    assert "12 of 22 subscription/check pair(s) were skipped" in out
    assert "resource provider is not registered" in out


def test_partial_failure_still_reports_normally(capsys):
    result = ScanResult(findings=[], subscriptions=["sub-1"], attempted=4)
    result.errors = [ScanError("sub-1", "x", "permission denied: no access")]
    render(result, Console(width=100, force_terminal=False))
    out = capsys.readouterr().out
    assert "No waste found" in out
    assert "Nothing could be scanned" not in out


def test_everything_filtered_out_is_not_no_waste(capsys):
    """'No waste found' after --min-cost hid 77 findings is a lie."""
    result = ScanResult(findings=[], subscriptions=["sub-1"], attempted=4)
    render(result, Console(width=100, force_terminal=False), hidden_by_filter=77)
    out = capsys.readouterr().out
    assert "77 finding(s) were hidden by the cost filter" in out
    assert "No waste found" not in out


def test_script_warns_before_the_first_command():
    result = ScanResult(findings=[_finding("x", "a", 5.0)], subscriptions=["sub-1"])
    script = to_script(result)
    assert script.index("READ EVERY LINE") < script.index("az thing delete")
    assert "did not run it" in script


def test_the_script_header_distinguishes_what_can_be_undone():
    """Azure has real recovery windows, and a header that flattened them would
    be wrong in both directions."""
    script = to_script(ScanResult(findings=[_finding("x", "a", 5.0)], subscriptions=["sub-1"]))
    assert "Key Vault and SQL keep a" in script
    assert "public IP address is gone for good" in script


def test_script_comments_cannot_become_commands():
    """A newline in a name would end the comment and execute what follows."""
    finding = Finding(
        check="x",
        resource_id="r\nrm -rf /",
        resource_type="t",
        subscription=SUBSCRIPTION,
        resource_group="rg\nrm -rf /",
        location="eastus",
        reason="line one\nrm -rf /",
        monthly_cost=1.0,
        remediation="az thing delete --name r",
    )
    script = to_script(ScanResult(findings=[finding], subscriptions=["sub-1"]))
    for line in script.splitlines():
        assert "rm -rf /" not in line or line.startswith("#")


# --------------------------------------------------------------------------
# the generated-command contract, checked at the source


HOSTILE_ID = "leftover'; echo PWNED; #"


def test_remediation_cannot_break_out_of_shell_quoting():
    """A quote in a resource name must not become a command in the plan.

    Azure's own naming rules make this unreachable through the disk API
    today. Relying on a remote service's input validation for local shell
    safety is the assumption that ages badly.
    """
    from tests.conftest import TEST_PRICES
    from zombiescan.models import ScanContext
    from zombiescan.packs.core.unattached_disk import build_finding
    from zombiescan.pricing import PriceTable

    ctx = ScanContext(arm=None, subscription="sub-1", pricing=PriceTable(TEST_PRICES))
    finding = build_finding(
        ctx,
        {
            "id": f"/subscriptions/sub-1/resourceGroups/rg/providers/"
            f"Microsoft.Compute/disks/{HOSTILE_ID}",
            "name": HOSTILE_ID,
            "location": "eastus",
            "resourceGroup": "rg",
            "sku": {"name": "Premium_LRS"},
            "properties": {"diskSizeGB": 128, "diskState": "Unattached"},
        },
    )

    # The property that matters: a shell parsing this line sees the hostile
    # name as one argument to the delete, and sees no extra commands.
    tokens = shlex.split(finding.remediation)
    assert tokens[:3] == ["az", "disk", "delete"]
    assert tokens[4] == HOSTILE_ID
    assert "echo" not in tokens
    assert ";" not in tokens


def _remediation_bodies():
    """Every `remediation=` expression in every pack, as source text."""
    import zombiescan.packs

    root = pathlib.Path(zombiescan.packs.__file__).parent
    for module in sorted(root.glob("*/*.py")):
        source = module.read_text()
        for match in re.finditer(r"remediation=\(?\s*((?:[^()]|\([^()]*\))*?)\),?\n", source):
            yield module.name, match.group(1)


def test_every_generated_command_goes_through_the_command_builders():
    """Two properties of every command a check emits, checked at the source.

    A command that names no subscription acts on whatever the operator's
    shell happens to be pointed at, which is the exact accident the report
    exists to prevent. And `az` has no global --quiet: `--yes` belongs only
    on the commands that would otherwise prompt, so it cannot be appended
    blindly. `helpers.az` settles both, and nothing should be building a
    command by hand instead.
    """
    offenders = []
    for name, body in _remediation_bodies():
        if "az " not in body:
            continue
        if "helpers.az(" in body or "helpers.az_resource_delete(" in body:
            continue
        offenders.append(f"{name}: {body.strip()[:70]}")
    assert offenders == [], (
        "commands not built by helpers.az, so not guaranteed to carry their "
        "subscription:\n" + "\n".join(offenders)
    )


def test_every_interpolated_name_is_shell_quoted():
    """An id spliced into a command without `helpers.arg` is a quoting hole."""
    offenders = []
    for name, body in _remediation_bodies():
        for interpolation in re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_.\[\]'\"]*)\}", body):
            if interpolation.startswith("helpers.arg(") or interpolation in (
                "ctx.subscription",
                "group",
            ):
                continue
            offenders.append(f"{name}: {{{interpolation}}}")
    assert offenders == [], "interpolations not wrapped in helpers.arg:\n" + "\n".join(offenders)
