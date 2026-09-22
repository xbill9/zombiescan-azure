"""The helpers more than one check depends on."""

from __future__ import annotations

import datetime as dt

import pytest

from zombiescan import azure, helpers

SUB = "00000000-1111-2222-3333-444444444444"
DISK_ID = f"/subscriptions/{SUB}/resourceGroups/Prod-RG/providers/Microsoft.Compute/disks/data-1"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("eastus", "eastus"),
        ("East US", "eastus"),
        ("westeurope", "westeurope"),
        ("global", "global"),
        ("", "global"),
    ],
)
def test_a_region_is_normalised_to_how_arm_spells_it(location, expected):
    """ARM answers `eastus`; a human writes `East US`. Both must price the same."""
    assert azure.region_of(location) == expected


def test_an_arm_id_carries_everything_a_command_needs():
    assert azure.subscription_of(DISK_ID) == SUB
    assert azure.resource_group_of(DISK_ID) == "Prod-RG"
    assert azure.name_of(DISK_ID) == "data-1"
    assert azure.type_of(DISK_ID) == "microsoft.compute/disks"


def test_resource_group_is_found_whatever_case_arm_used():
    """ARM returns `resourceGroups`; Resource Graph returns `resourcegroups`."""
    lowered = DISK_ID.replace("resourceGroups", "resourcegroups")
    assert azure.resource_group_of(lowered) == "Prod-RG"


def test_a_sub_resource_type_is_read_from_the_whole_path():
    subnet = (
        f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Network"
        "/virtualNetworks/vnet/subnets/web"
    )
    assert azure.type_of(subnet) == "microsoft.network/virtualnetworks/subnets"
    assert azure.name_of(subnet) == "web"


def test_an_api_version_falls_back_to_the_parent_type():
    """Azure versions a sub-type with its parent, so there is no list to keep."""
    assert (
        azure.api_version("Microsoft.Sql/servers/databases")
        == azure.API_VERSIONS["Microsoft.Sql/servers"]
    )
    assert (
        azure.api_version("Microsoft.Network/virtualNetworks/subnets")
        == (azure.API_VERSIONS["Microsoft.Network/virtualNetworks"])
    )


def test_an_unpinned_type_names_the_ones_that_are():
    with pytest.raises(KeyError, match="API_VERSIONS"):
        azure.api_version("Microsoft.Nonesuch/widgets")


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:00:00.1234567Z",  # ARM's seven digits
        "2026-09-01T00:00:00.123+00:00",
        "2026-09-01T00:00:00",  # no zone at all
    ],
)
def test_every_timestamp_shape_arm_returns_parses(timestamp):
    assert helpers.age_days(timestamp) is not None


def test_an_unparseable_timestamp_is_unknown_rather_than_zero():
    assert helpers.age_days("not a date") is None
    assert helpers.age_days(None) is None


def test_key_vault_reports_its_times_as_epoch_seconds():
    """Alone among the services scanned here."""
    week_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
    assert helpers.epoch_age_days(week_ago.timestamp()) == 7
    assert helpers.epoch_age_days(None) is None


def test_properties_reads_both_shapes_a_resource_arrives_in():
    """ARM nests under `properties`; a projected Graph row does not."""
    assert helpers.properties({"properties": {"state": "Idle"}})["state"] == "Idle"
    assert helpers.properties({"state": "Idle"})["state"] == "Idle"


def test_arm_ids_are_compared_lowercased():
    """ARM and Resource Graph disagree on case, and a case-sensitive
    comparison finds nothing and reports every resource as unused."""
    assert helpers.arm_ids([{"id": DISK_ID}]) == {DISK_ID.lower()}


# --- generated commands ---------------------------------------------------


def test_a_command_that_prompts_gets_yes():
    assert helpers.az("az disk delete --name d", "sub-1", "rg-1") == (
        "az disk delete --name d --resource-group rg-1 --subscription sub-1 --yes"
    )


def test_a_command_that_does_not_prompt_does_not_get_yes():
    """`az` has no global --quiet: passing --yes to a command that does not
    take it is an error, not a no-op."""
    assert helpers.az("az network nic delete --name n", "sub-1", "rg-1") == (
        "az network nic delete --name n --resource-group rg-1 --subscription sub-1"
    )


def test_a_command_with_no_resource_group_still_names_its_subscription():
    assert helpers.az("az group delete --name rg-1", "sub-1") == (
        "az group delete --name rg-1 --subscription sub-1 --yes"
    )


def test_the_verb_is_matched_before_the_flags():
    """`az disk delete --resource-group x` must still be recognised as prompting."""
    assert helpers._prompts("az disk delete --name d")
    assert helpers._prompts("disk delete")
    assert not helpers._prompts("az network lb delete --name l")


def test_a_name_with_shell_metacharacters_stays_one_argument():
    assert helpers.arg("a;rm -rf /") == "'a;rm -rf /'"
    assert helpers.arg("ordinary-name") == "ordinary-name"
    # Resource group names allow parentheses and periods, which most Azure
    # names do not.
    assert helpers.arg("rg (old).backup") == "'rg (old).backup'"


def test_a_resource_delete_covers_what_core_az_has_no_verb_for():
    assert helpers.az_resource_delete("/subscriptions/s/x", "sub-1") == (
        "az resource delete --ids /subscriptions/s/x --subscription sub-1"
    )


# --- what an `az` failure tells the operator ------------------------------


def test_a_failure_that_is_not_about_signing_in_does_not_say_to_sign_in():
    """`az` writes a usable sentence; burying it under a wrong guess sends the
    operator to fix something that is not broken."""
    message = azure._az_failure(
        ["account", "get-access-token"],
        "ERROR: Subscription 'x' not found. Check the spelling and casing and try again.",
    )
    assert message == (
        "Azure CLI: Subscription 'x' not found. Check the spelling and casing and try again."
    )
    assert "az login" not in message


def test_a_sign_in_failure_does_say_to_sign_in():
    message = azure._az_failure(
        ["account", "show"],
        "ERROR: Please run 'az login' to setup account.",
    )
    assert "Run 'az login' first." in message


def test_an_expired_token_is_a_sign_in_failure():
    message = azure._az_failure(
        ["account", "get-access-token"], "AADSTS700082: refresh token expired"
    )
    assert "Run 'az login' first." in message


def test_a_silent_failure_still_says_something():
    assert azure._az_failure(["account", "show"], "") == "Azure CLI: no output."
