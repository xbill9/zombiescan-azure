from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from zombiescan.packs.core.disabled_kms_key import disabled_kms_key

FIXTURE = load_fixture("disabled_kms_key")


@pytest.fixture
def findings(make_context):
    ctx, _ = make_context(
        {
            "projects.locations.list": FIXTURE["locations"],
            "projects.locations.keyRings.list": lambda **kw: FIXTURE["keyRings"][kw["parent"]],
            "projects.locations.keyRings.cryptoKeys.list": (
                lambda **kw: FIXTURE["cryptoKeys"][kw["parent"]]
            ),
            "projects.locations.keyRings.cryptoKeys.cryptoKeyVersions.list": (
                lambda **kw: FIXTURE["versions"][kw["parent"]]
            ),
        }
    )
    return {f.resource_id: f for f in disabled_kms_key(ctx)}


def test_a_disabled_version_is_flagged(findings):
    """Disabling does not stop the charge; only destroying does."""
    assert "app-ring/retired-signing-key/1" in findings


def test_an_enabled_version_is_not_waste(findings):
    assert "app-ring/live-key/5" not in findings


def test_a_destroyed_version_is_already_gone(findings):
    assert "app-ring/retired-signing-key/2" not in findings


def test_every_kms_location_is_walked(findings):
    """Cloud KMS rejects locations/-, so the check has to enumerate them itself."""
    assert "eu-ring/hsm-key/3" in findings
    assert findings["eu-ring/hsm-key/3"].location == "europe-west1"


def test_an_hsm_version_is_priced_at_the_hsm_rate(findings):
    """HSM key versions cost far more than software ones."""
    assert findings["eu-ring/hsm-key/3"].monthly_cost == pytest.approx(1.10)
    assert findings["app-ring/retired-signing-key/1"].monthly_cost == pytest.approx(0.06)


def test_the_reason_says_disabling_is_not_enough(findings):
    reason = findings["app-ring/retired-signing-key/1"].reason
    assert "only destroying it does" in reason


def test_the_note_records_the_recovery_window(findings):
    note = findings["app-ring/retired-signing-key/1"].details["note"]
    assert "24 hours" in note


def test_the_destroy_command_carries_the_whole_key_path(findings):
    remediation = findings["eu-ring/hsm-key/3"].remediation
    assert remediation == (
        "gcloud kms keys versions destroy 3 --key=hsm-key --keyring=eu-ring "
        "--location=europe-west1 --project=test-project --quiet"
    )
