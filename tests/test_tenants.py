"""Spanning two tenants: one token each, and every request routed to the right one.

The shape this exists for is ordinary rather than exotic. Microsoft lets one
email address be both a work or school account and a personal Microsoft
account, and they are separate directories with separate subscriptions. An ARM
token is issued for a single tenant, so a scan that held one would quietly
cover half of what the operator can see and report less waste than there is.
"""

from __future__ import annotations

import pytest

from zombiescan import azure
from zombiescan.azure import Arm, Credential, Subscription

WORK_TENANT = "11111111-1111-1111-1111-111111111111"
PERSONAL_TENANT = "22222222-2222-2222-2222-222222222222"

WORK = Subscription("sub-work", "work", WORK_TENANT, "Enabled", "me@example.com")
WORK_TWO = Subscription("sub-work-2", "work-2", WORK_TENANT, "Enabled", "me@example.com")
PERSONAL = Subscription("sub-personal", "personal", PERSONAL_TENANT, "Enabled", "me@example.com")
DISABLED = Subscription("sub-old", "old", PERSONAL_TENANT, "Disabled", "me@example.com")

EVERY = [WORK, WORK_TWO, PERSONAL, DISABLED]


class FakeCredential:
    """Records which token each request asked for, and how tokens were minted."""

    def __init__(self):
        self.warmed: list[tuple[str, str | None]] = []
        self.asked: list[str] = []

    def warm(self, tenant, subscription=None):
        self.warmed.append((tenant, subscription))

    def token(self, tenant=azure.DEFAULT_TENANT):
        self.asked.append(tenant)
        return f"token-for-{tenant or 'default'}"


@pytest.fixture
def arm():
    return Arm(FakeCredential(), EVERY)


# --- routing --------------------------------------------------------------


def test_a_subscription_is_routed_to_its_own_tenant(arm):
    assert arm.tenant_of("sub-work") == WORK_TENANT
    assert arm.tenant_of("sub-personal") == PERSONAL_TENANT


def test_subscriptions_in_one_tenant_share_a_token(arm):
    """A tenant is the unit a token is issued for, not a subscription."""
    assert arm.token_source("sub-work")[0] == arm.token_source("sub-work-2")[0]
    assert arm.token_source("sub-work")[0] != arm.token_source("sub-personal")[0]


def test_a_tenants_token_is_minted_from_one_of_its_subscriptions(arm):
    """`az` resolves the tenant *and* the identity that owns it from the
    subscription, so naming one is better than guessing either."""
    key, source = arm.token_source("sub-work-2")
    assert key == WORK_TENANT
    assert source in ("sub-work", "sub-work-2")


def test_a_subscription_az_has_never_seen_gets_a_token_of_its_own(arm):
    """Passed with --subscription before it was ever logged into. Keying it on
    the default tenant would send someone else's token."""
    key, source = arm.token_source("sub-unknown")
    assert key == "subscription:sub-unknown"
    assert source == "sub-unknown"
    assert key != azure.DEFAULT_TENANT


def test_the_subscription_lookup_ignores_case(arm):
    """ARM returns ids in one case and Resource Graph in another."""
    assert arm.tenant_of("SUB-WORK") == WORK_TENANT


# --- warming --------------------------------------------------------------


def test_prepare_fetches_one_token_per_tenant_not_per_subscription(arm):
    arm.prepare(["sub-work", "sub-work-2", "sub-personal"])
    tenants = [tenant for tenant, _ in arm._credential.warmed]
    assert sorted(set(tenants)) == sorted([WORK_TENANT, PERSONAL_TENANT])
    assert len(tenants) == 3, "warm is called per subscription; the cache dedupes"


# --- which token each request actually carries ----------------------------


def test_a_request_takes_its_tenant_from_the_path(arm, monkeypatch):
    sent = []
    monkeypatch.setattr(
        Arm, "_send", lambda self, method, url, body, tenant: sent.append(tenant) or {}
    )
    arm.request("GET", "/subscriptions/sub-personal/providers/Microsoft.Compute/disks", "v")
    arm.request("GET", "/subscriptions/sub-work/providers/Microsoft.Compute/disks", "v")
    assert sent == [PERSONAL_TENANT, WORK_TENANT]


def test_a_next_link_keeps_the_tenant_of_the_call_that_started_it(arm, monkeypatch):
    """Page two is a full URL. It must not fall back to the default token."""
    pages = [
        {
            "value": [{"name": "a"}],
            "nextLink": "https://management.azure.com/subscriptions/sub-personal/x?skip=1",
        },
        {"value": [{"name": "b"}]},
    ]
    sent = []

    def fake_send(self, method, url, body, tenant):
        sent.append(tenant)
        return pages.pop(0)

    monkeypatch.setattr(Arm, "_send", fake_send)
    items = list(
        arm.list(
            "/subscriptions/sub-personal/providers/Microsoft.Compute/disks",
            "Microsoft.Compute/disks",
        )
    )
    assert [i["name"] for i in items] == ["a", "b"]
    assert sent == [PERSONAL_TENANT, PERSONAL_TENANT]


def test_resource_graph_names_its_tenant_because_the_path_does_not(arm, monkeypatch):
    """Its path carries no subscription -- that is in the body -- so deriving
    one would silently send the default tenant's token."""
    sent = []

    def fake_send(self, method, url, body, tenant):
        sent.append(tenant)
        return {"data": []}

    monkeypatch.setattr(Arm, "_send", fake_send)
    list(arm.graph("Resources | limit 1", "sub-personal"))
    assert sent == [PERSONAL_TENANT]


def test_a_path_with_no_subscription_uses_the_default_token(arm, monkeypatch):
    sent = []
    monkeypatch.setattr(
        Arm, "_send", lambda self, method, url, body, tenant: sent.append(tenant) or {}
    )
    arm.request("GET", "/providers/Microsoft.Something/else", "v")
    assert sent == [azure.DEFAULT_TENANT]


# --- the token cache ------------------------------------------------------


def test_a_token_is_fetched_once_per_tenant_and_then_reused(monkeypatch):
    calls = []

    def fake_run_az(args):
        calls.append(args)
        return {"accessToken": f"t{len(calls)}", "expires_on": 9999999999}

    monkeypatch.setattr(azure, "_run_az", fake_run_az)
    credential = Credential()  # the default token
    credential.warm(WORK_TENANT, "sub-work")
    credential.warm(PERSONAL_TENANT, "sub-personal")

    assert len(calls) == 3
    for _ in range(5):
        credential.token(WORK_TENANT)
        credential.token(PERSONAL_TENANT)
    assert len(calls) == 3, "a cached token was re-fetched"
    assert credential.token(WORK_TENANT) != credential.token(PERSONAL_TENANT)


def test_warming_a_tenant_twice_does_not_fetch_twice(monkeypatch):
    calls = []
    monkeypatch.setattr(
        azure,
        "_run_az",
        lambda args: calls.append(args) or {"accessToken": "t", "expires_on": 9999999999},
    )
    credential = Credential()
    credential.warm(WORK_TENANT, "sub-work")
    credential.warm(WORK_TENANT, "sub-work")
    assert len(calls) == 2, "the second warm of the same tenant shelled out again"


def test_an_expired_token_is_refreshed_from_its_own_subscription(monkeypatch):
    minted = []

    def fake_run_az(args):
        minted.append(args[-1] if "--subscription" in args else None)
        # Long expired, so the next read refreshes. Not zero: `_fetch` reads a
        # zero as "az did not say" and defaults to an hour, which is the right
        # thing for a missing expiry and the wrong thing for this test.
        return {"accessToken": "t", "expires_on": 1}

    monkeypatch.setattr(azure, "_run_az", fake_run_az)
    credential = Credential()
    credential.warm(PERSONAL_TENANT, "sub-personal")
    minted.clear()
    credential.token(PERSONAL_TENANT)
    assert minted == ["sub-personal"], "refreshed against the wrong subscription"


def test_the_tenants_a_credential_holds_are_reportable(monkeypatch):
    monkeypatch.setattr(
        azure, "_run_az", lambda args: {"accessToken": "t", "expires_on": 9999999999}
    )
    credential = Credential()
    credential.warm(WORK_TENANT, "sub-work")
    assert credential.tenants == sorted([azure.DEFAULT_TENANT, WORK_TENANT])


# --- what az reports ------------------------------------------------------


def test_known_subscriptions_reads_every_tenant_az_has_signed_into(monkeypatch):
    monkeypatch.setattr(
        azure,
        "_run_az",
        lambda args: [
            {
                "id": "sub-work",
                "name": "work",
                "tenantId": WORK_TENANT,
                "state": "Enabled",
                "user": {"name": "me@example.com"},
            },
            {
                "id": "sub-personal",
                "name": "personal",
                "tenantId": PERSONAL_TENANT,
                "state": "Enabled",
                "user": {"name": "me@example.com"},
            },
            # No tenant: not something a token can be issued for.
            {"id": "sub-broken", "name": "broken", "state": "Enabled"},
        ],
    )
    found = azure.known_subscriptions()
    assert [s.id for s in found] == ["sub-work", "sub-personal"]
    assert {s.tenant_id for s in found} == {WORK_TENANT, PERSONAL_TENANT}


def test_refresh_is_asked_for_only_when_requested(monkeypatch):
    seen = []
    monkeypatch.setattr(azure, "_run_az", lambda args: seen.append(args) or [])
    azure.known_subscriptions()
    azure.known_subscriptions(refresh=True)
    assert "--refresh" not in seen[0]
    assert "--refresh" in seen[1]


def test_an_expiry_az_did_not_report_is_not_read_as_already_expired(monkeypatch):
    """A missing `expires_on` means unknown, not zero. Treating it as expired
    would put an `az` process on every request."""
    calls = []
    monkeypatch.setattr(azure, "_run_az", lambda args: calls.append(args) or {"accessToken": "t"})
    credential = Credential()
    for _ in range(5):
        credential.token()
    assert len(calls) == 1


def test_the_first_token_is_reused_for_its_own_tenant(monkeypatch):
    """`az` names the tenant it issued a token for, so the one fetched up
    front to check the operator is signed in is already that tenant's. Asking
    for it again would be a second `az` process for nothing."""
    calls = []

    def fake_run_az(args):
        calls.append(args)
        return {"accessToken": "t", "expires_on": 9999999999, "tenant": WORK_TENANT}

    monkeypatch.setattr(azure, "_run_az", fake_run_az)
    credential = Credential()  # no subscription named: the default token
    assert len(calls) == 1

    credential.warm(WORK_TENANT, "sub-work")
    assert len(calls) == 1, "re-fetched a token az had already issued for this tenant"
    assert credential.token(WORK_TENANT) == credential.token()


def test_a_second_tenant_still_gets_its_own_token(monkeypatch):
    """The aliasing must not swallow the tenant that genuinely needs a fetch."""
    issued = [WORK_TENANT, PERSONAL_TENANT]
    calls = []

    def fake_run_az(args):
        calls.append(args)
        return {
            "accessToken": f"t{len(calls)}",
            "expires_on": 9999999999,
            "tenant": issued[min(len(calls) - 1, len(issued) - 1)],
        }

    monkeypatch.setattr(azure, "_run_az", fake_run_az)
    credential = Credential()
    credential.warm(WORK_TENANT, "sub-work")
    credential.warm(PERSONAL_TENANT, "sub-personal")

    assert len(calls) == 2
    assert credential.token(WORK_TENANT) != credential.token(PERSONAL_TENANT)


# --- telling a personal account from a work one ----------------------------


def _jwt(claims):
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


@pytest.mark.parametrize(
    "claims, kind",
    [
        ({"idp": "live.com", "tid": PERSONAL_TENANT, "scp": "user_impersonation"}, azure.PERSONAL),
        ({"tid": WORK_TENANT, "upn": "me@example.com", "scp": "user_impersonation"}, azure.WORK),
        (
            {"idp": f"https://sts.windows.net/{WORK_TENANT}/", "scp": "user_impersonation"},
            azure.GUEST,
        ),
        ({"idtyp": "app", "appid": "app-id"}, azure.SERVICE_PRINCIPAL),
        ({}, ""),
    ],
)
def test_the_kind_of_account_is_read_off_the_token(claims, kind):
    """`az account list` shows a personal and a work account as the same user;
    only the token's `idp` claim tells them apart."""
    assert azure.account_kind(azure._claims(_jwt(claims))) == kind


def test_a_token_that_is_not_a_jwt_has_no_kind():
    assert azure.account_kind(azure._claims("opaque")) == ""


def test_each_tenant_reports_the_kind_of_account_that_holds_its_token(monkeypatch):
    tokens = {
        None: (_jwt({"tid": WORK_TENANT, "scp": "x"}), WORK_TENANT),
        "sub-personal": (_jwt({"idp": "live.com", "scp": "x"}), PERSONAL_TENANT),
    }

    def fake_run_az(args):
        subscription = args[args.index("--subscription") + 1] if "--subscription" in args else None
        value, tenant = tokens[subscription]
        return {"accessToken": value, "expires_on": 9999999999, "tenant": tenant}

    monkeypatch.setattr(azure, "_run_az", fake_run_az)
    arm = Arm(Credential(), [WORK, PERSONAL])
    arm.prepare(["sub-personal"])

    assert arm.account_kind("sub-work") == azure.WORK
    assert arm.account_kind("sub-personal") == azure.PERSONAL


def test_az_account_list_carries_the_directory_name_and_default(monkeypatch):
    rows = [
        {
            "id": "sub-personal",
            "name": "Azure subscription 1",
            "tenantId": PERSONAL_TENANT,
            "tenantDisplayName": "Default Directory",
            "tenantDefaultDomain": "meexamplecom.onmicrosoft.com",
            "state": "Enabled",
            "isDefault": True,
            "user": {"name": "me@example.com", "type": "user"},
        }
    ]
    monkeypatch.setattr(azure, "_run_az", lambda args: rows)
    (found,) = azure.known_subscriptions()
    assert found.tenant_name == "Default Directory"
    assert found.tenant_domain == "meexamplecom.onmicrosoft.com"
    assert found.is_default
