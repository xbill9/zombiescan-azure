#!/usr/bin/env python3
"""Every check run against the test suite's recorded ARM responses, priced from
the real bundled price table.

The recorded responses are the ones `tests/` drives each check with, wired the
same way each test file wires them. Only the prices differ: the suite uses a
small fixed table, and this uses `table.json`, so each figure is what that
resource would cost at today's list price.

    uv run python articles/zombiescan-azure/show-fixture-findings.py
"""

from __future__ import annotations

import importlib
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tests.conftest import FakeArm  # noqa: E402

from zombiescan import engine  # noqa: E402
from zombiescan.models import ScanContext  # noqa: E402
from zombiescan.pricing import PriceTable  # noqa: E402
from zombiescan.registry import CHECKS  # noqa: E402

SUBSCRIPTION = "00000000-1111-2222-3333-444444444444"


def main() -> int:
    engine.load_packs()
    pricing = PriceTable.load()

    def make_context(responses=None, graph=None, subscription=SUBSCRIPTION, providers=frozenset()):
        arm = FakeArm(responses, graph)
        return ScanContext(
            arm=arm, subscription=subscription, pricing=pricing, providers=providers
        ), arm

    rows = []
    for name in sorted(CHECKS):
        module = importlib.import_module(f"tests.test_{name.replace('-', '_')}")
        result = module._findings(make_context)
        findings = result[0] if isinstance(result, tuple) else result
        findings = list(findings.values()) if isinstance(findings, dict) else list(findings)
        rows += findings

    rows.sort(key=lambda f: -f.monthly_cost)
    total = sum(f.monthly_cost for f in rows)
    print(
        f"{len(rows)} findings from {len(CHECKS)} checks, recorded responses, "
        f"prices generated {pricing.generated}"
    )
    print(f"total ${total:,.2f}/month\n")
    print(f"{'Check':34} {'Resource':30} {'Monthly':>11}")
    for f in rows:
        mark = "~" if f.approximate_cost else " "
        print(f"{f.check:34} {f.resource_id[:30]:30} {f'${f.monthly_cost:,.2f}':>10}{mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
