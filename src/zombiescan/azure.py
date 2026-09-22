"""The Azure client layer.

Credentials come from the ``az`` CLI and every call goes to one endpoint --
Azure Resource Manager -- so adding coverage for a new service is an entry in
``API_VERSIONS`` rather than a new dependency. That uniformity is what lets
``building.simple_check`` and the ``Step`` runner in ``clean.py`` drive any
service without knowing which one they are talking to.

Three facts about Azure shape everything above this module:

* **Azure Resource Graph answers for every region and resource group at
  once.** One KQL query returns disks, public IPs, NICs, load balancers --
  across every resource group in a subscription, in a single call. So a check
  is scoped to a *subscription*, not to a region, and fanning out per region
  would turn one call into sixty.
* **ARM is one REST surface.** Every provider lives under
  ``management.azure.com`` and differs only in its path and its pinned
  ``api-version``. There is no per-service client to build and nothing to
  discover.
* **An unregistered resource provider returns an empty list, not an error.**
  ``GET .../providers/Microsoft.Web/serverfarms`` on a subscription that has
  never used App Service answers ``{"value": []}`` with HTTP 200. A check run
  against it finds nothing and the scan reports a clean subscription, which is
  the worst failure this tool has: a false all-clear. ``registered_providers``
  asks which namespaces are registered once per subscription, and the engine
  marks a check whose provider is missing as unavailable rather than running
  it. **Never remove that pre-check on the grounds that the call "works".**

``Finding.location`` still records the region each resource lives in, because
that is what the operator needs, and ``Finding.resource_group`` records the
group, because no ``az`` delete command works without it.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

ARM = "https://management.azure.com"

# Which api-version to send for each resource type. Pinned rather than
# "latest" because Azure has no "latest": the version is a required query
# parameter, and a newer one can change a property's shape under a check
# silently. Resolved against the longest matching prefix, so a sub-type
# inherits its parent's version unless it names its own.
API_VERSIONS = {
    "Microsoft.Compute/disks": "2026-03-02",
    "Microsoft.Compute/snapshots": "2026-03-02",
    "Microsoft.Compute/images": "2026-04-01",
    "Microsoft.Compute/virtualMachines": "2026-04-01",
    "Microsoft.ContainerRegistry/registries": "2025-11-01",
    "Microsoft.ContainerService/managedClusters": "2026-06-01",
    "Microsoft.Insights/webtests": "2022-06-15",
    "Microsoft.KeyVault/vaults": "2026-05-15",
    "Microsoft.Network/dnszones": "2018-05-01",
    "Microsoft.Network/loadBalancers": "2026-03-01",
    "Microsoft.Network/natGateways": "2026-03-01",
    "Microsoft.Network/networkInterfaces": "2026-03-01",
    "Microsoft.Network/networkSecurityGroups": "2026-03-01",
    "Microsoft.Network/privateDnsZones": "2024-06-01",
    "Microsoft.Network/publicIPAddresses": "2026-03-01",
    "Microsoft.Network/virtualNetworks": "2026-03-01",
    "Microsoft.OperationalInsights/workspaces": "2026-03-01",
    "Microsoft.Sql/servers": "2025-01-01",
    "Microsoft.Storage/storageAccounts": "2026-06-01",
    "Microsoft.Web/serverfarms": "2026-08-01",
    # Not a provider a check declares: the control-plane calls the engine
    # itself makes. Resource Manager's own collections are addressed directly
    # under a subscription rather than under /providers/ -- see
    # ``ScanContext.provider_path``.
    "Microsoft.Resources/resourceGroups": "2023-07-01",
    "Microsoft.Resources/providers": "2021-04-01",
    "Microsoft.Resources/subscriptions": "2022-12-01",
    "Microsoft.ResourceGraph/resources": "2024-04-01",
}

GLOBAL = "global"

# Azure Resource Graph returns at most this many rows per page and pages with
# a $skipToken. Its own maximum is 1000.
GRAPH_PAGE = 1000

# ARM throttles per-subscription reads and Resource Graph throttles per-user.
# Both answer 429 with a Retry-After, so the polite thing and the correct
# thing are the same.
MAX_ATTEMPTS = 4


class CredentialError(RuntimeError):
    """No usable credentials. The message names the fix."""


class ProviderNotRegistered(RuntimeError):
    """The resource provider is not registered on this subscription.

    Not a failure: a subscription that has never used App Service has no App
    Service waste. Raised by the engine's pre-check rather than by ARM, which
    answers an unregistered provider's list call with an empty page.
    """


class ArmError(RuntimeError):
    """One failed ARM request, with the parts a caller can act on."""

    def __init__(self, status: int, code: str, message: str, url: str = "") -> None:
        super().__init__(f"{code or status}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.url = url


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def _run_az(args: list[str]) -> Any:
    """One ``az`` invocation returning JSON, or a credential error naming the fix."""
    try:
        completed = subprocess.run(
            ["az", *args, "--output", "json", "--only-show-errors"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise CredentialError(
            "The 'az' command was not found. Install the Azure CLI and run 'az login' first."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CredentialError(f"'az {' '.join(args)}' did not return within 120 seconds.") from exc

    if completed.returncode != 0:
        raise CredentialError(_az_failure(args, completed.stderr or completed.stdout or ""))
    try:
        return json.loads(completed.stdout or "null")
    except json.JSONDecodeError as exc:
        raise CredentialError(f"'az {' '.join(args)}' returned output that is not JSON.") from exc


# Words in an ``az`` failure that mean the operator is not signed in. Anything
# else -- a subscription that does not exist, a tenant they cannot reach -- is
# already explained by the message ``az`` printed, and telling them to log in
# on top of it sends them to fix something that is not broken.
_SIGN_IN_HINTS = (
    "az login",
    "not logged in",
    "please run",
    "refresh token",
    "expired",
    "credential",
    "aadsts",
)


def _az_failure(args: list[str], output: str) -> str:
    """What to tell the operator when an ``az`` call fails.

    ``az`` writes a usable sentence of its own -- "Subscription 'x' not found.
    Check the spelling and casing and try again." -- so the job here is to pass
    it on rather than to bury it under a guess. The login hint is added only
    when the failure actually looks like one.
    """
    lines = [line for line in output.strip().splitlines() if line.strip()]
    detail = lines[0].strip() if lines else "no output"
    detail = detail.removeprefix("ERROR:").strip().rstrip(".")
    message = f"Azure CLI: {detail}."
    if any(hint in detail.lower() for hint in _SIGN_IN_HINTS):
        message += " Run 'az login' first."
    return message


# The cache key for the token that ``az`` hands out when no subscription is
# named. Everything reachable from one tenant shares one token; a tenant is the
# unit a token is issued for.
DEFAULT_TENANT = ""


@dataclass(frozen=True)
class Subscription:
    """One subscription the ``az`` CLI knows about, and the tenant it sits in."""

    id: str
    name: str
    tenant_id: str
    state: str
    user: str

    @property
    def enabled(self) -> bool:
        return self.state == "Enabled"


def known_subscriptions(refresh: bool = False) -> list[Subscription]:
    """Every subscription the ``az`` CLI holds credentials for, across tenants.

    **This is deliberately not ARM's own ``/subscriptions``.** An ARM token is
    issued for one tenant and can only list that tenant's subscriptions, so a
    scan driven from it silently covers whichever tenant happened to be
    current. The same person very often has two: Microsoft lets one email
    address be both a work or school account and a personal Microsoft account,
    and they are separate directories with separate subscriptions.

    ``az`` tracks every identity that has signed in, so its own list is the one
    that spans them. The trade is that it reflects what the CLI has seen rather
    than what exists right now -- a subscription created since the last login
    is missing until ``--refresh`` re-queries each tenant, which is why
    ``--all-subscriptions`` pays that cost and an ordinary scan does not.
    """
    args = ["account", "list", "--all"]
    if refresh:
        args.append("--refresh")
    rows = _run_az(args) or []
    found = []
    for row in rows:
        identity = row.get("user") or {}
        if row.get("id") and row.get("tenantId"):
            found.append(
                Subscription(
                    id=row["id"],
                    name=row.get("name") or row["id"],
                    tenant_id=row["tenantId"],
                    state=row.get("state") or "Unknown",
                    user=identity.get("name") or "",
                )
            )
    return found


@dataclass
class _Token:
    value: str
    expires: float
    # Which tenant `az` said it issued this for. The response names it, so a
    # token fetched under one key can be filed under its real tenant too
    # rather than fetched a second time.
    tenant: str = ""


class Credential:
    """ARM access tokens, sourced from the ``az`` CLI, one per tenant.

    **A token is issued for a single tenant.** One will not read another
    tenant's subscriptions, so a scan that spans two -- which is ordinary, and
    the normal shape when one email address is both a work account and a
    personal one -- needs one token each. They are cached by tenant and minted
    by naming a subscription that lives in it, because ``az`` resolves the
    tenant *and* the identity that owns it from the subscription, and guessing
    either here would be guessing at something ``az`` already knows.

    **Every token is fetched before any worker thread exists**, by
    ``Arm.prepare``. Every request asks for one, so without that the start of a
    scan would have every worker shelling out to ``az`` at the same moment.
    Concurrent ``az account get-access-token`` processes contend on the MSAL
    token cache in ``~/.azure`` and can leave it corrupt, which costs the
    operator an ``az login`` rather than a retry. The lock still guards
    refreshes, so an expiry part-way through a long scan refreshes exactly once
    instead of once per waiting thread.

    Holding tokens rather than shelling out per call is also what makes a scan
    quick: one ``az`` process per tenant for a whole run, not one per request.
    """

    # Refresh this many seconds before the token actually expires, so a long
    # request cannot start with a token that dies mid-flight.
    SKEW = 300

    def __init__(self, subscription: str | None = None) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, _Token] = {}
        # Which subscription to name when minting each tenant's token.
        self._sources: dict[str, str | None] = {}
        # Single-threaded, before any worker exists.
        self.warm(DEFAULT_TENANT, subscription)

    def _expiring(self, token: _Token) -> bool:
        return time.time() >= token.expires - self.SKEW

    def _fetch(self, subscription: str | None) -> _Token:
        args = ["account", "get-access-token", "--resource", f"{ARM}/"]
        if subscription:
            args += ["--subscription", subscription]
        payload = _run_az(args)
        value = (payload or {}).get("accessToken")
        if not value:
            raise CredentialError(
                "'az account get-access-token' returned no token. Run 'az login' first."
            )
        expires = float(payload.get("expires_on") or 0) or time.time() + 3600
        return _Token(value=value, expires=expires, tenant=str(payload.get("tenant") or ""))

    def warm(self, tenant: str, subscription: str | None = None) -> None:
        """Fetch this tenant's token now, on the calling thread.

        Called once per tenant before the worker pool starts. Doing it here is
        what keeps every worker from shelling out to ``az`` at once.

        The token ``az`` returns names the tenant it was issued for, so it is
        filed under that as well as under the key it was asked for. The first
        token -- fetched with no subscription named, to fail fast if nobody is
        signed in -- is therefore already the default tenant's, and asking for
        that tenant again costs nothing.
        """
        with self._lock:
            if subscription is not None or tenant not in self._sources:
                self._sources[tenant] = subscription
            current = self._tokens.get(tenant)
            if current is None or self._expiring(current):
                self._tokens[tenant] = self._fetch(self._sources[tenant])
            self._file_under_its_own_tenant(self._tokens[tenant], subscription)

    def _file_under_its_own_tenant(self, token: _Token, subscription: str | None) -> None:
        """Also cache a token under the tenant ``az`` says it belongs to."""
        if not token.tenant or token.tenant in self._tokens:
            return
        self._tokens[token.tenant] = token
        self._sources.setdefault(token.tenant, subscription)

    def token(self, tenant: str = DEFAULT_TENANT) -> str:
        current = self._tokens.get(tenant)
        if current is not None and not self._expiring(current):
            return current.value
        with self._lock:
            # Another thread may have refreshed while this one waited.
            current = self._tokens.get(tenant)
            if current is None or self._expiring(current):
                self._tokens[tenant] = self._fetch(self._sources.get(tenant))
            return self._tokens[tenant].value

    @property
    def tenants(self) -> list[str]:
        """Every tenant a token is held for. For the report and for tests."""
        return sorted(self._tokens)


def signed_in_account() -> dict[str, Any]:
    """What ``az`` is currently signed in as, and the subscription it defaults to.

    Whatever ``az account set`` last chose, read from the CLI's own config: it
    costs one local ``az`` call, no network, and tells the report who produced
    it so a later ``clean --from`` can refuse a report produced by somebody
    else.
    """
    return _run_az(["account", "show"]) or {}


def principal_of(account: dict[str, Any]) -> str:
    """Who these credentials belong to: a user, a service principal, or neither."""
    user = account.get("user") or {}
    name = user.get("name")
    if isinstance(name, str) and name:
        return name
    return "the signed-in az account"


def default_credentials(subscription: str | None = None) -> tuple[Credential, str, str | None]:
    """A token, the principal it belongs to, and the subscription ``az`` defaults to."""
    account = signed_in_account()
    if not account:
        raise CredentialError("No Azure account is signed in. Run 'az login' first.")
    credential = Credential(subscription)
    return credential, principal_of(account), account.get("id")


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def api_version(resource_type: str) -> str:
    """The pinned api-version for a resource type, or its nearest parent's.

    ``Microsoft.Sql/servers/databases`` resolves to the version pinned for
    ``Microsoft.Sql/servers``, because Azure versions a sub-type with its
    parent and duplicating every one of them here would be a list to keep in
    step rather than a decision to record.
    """
    parts = resource_type.split("/")
    for cut in range(len(parts), 0, -1):
        version = API_VERSIONS.get("/".join(parts[:cut]))
        if version:
            return version
    known = ", ".join(sorted(API_VERSIONS))
    raise KeyError(
        f"no api-version pinned for {resource_type!r}. Add it to azure.API_VERSIONS. Known: {known}"
    )


class Arm:
    """Azure Resource Manager, for one set of credentials.

    Unlike the per-service clients an SDK would hand out, there is one of
    these for a whole scan and it is safe to use from every worker thread:
    each request opens its own connection and the only shared state is the
    token, which ``Credential`` guards. That is the direct benefit of ARM
    being a single REST surface -- there is no per-service object holding a
    connection that two threads could corrupt.
    """

    def __init__(
        self,
        credential: Credential,
        subscriptions: Iterable[Subscription] = (),
        timeout: float = 60.0,
    ) -> None:
        self._credential = credential
        self._timeout = timeout
        self._providers: dict[str, frozenset[str]] = {}
        self._providers_lock = threading.Lock()
        self.known = list(subscriptions)
        self._tenant_of = {s.id.lower(): s.tenant_id for s in self.known}
        # One subscription per tenant is enough to mint that tenant's token.
        self._representative: dict[str, str] = {}
        for subscription in self.known:
            self._representative.setdefault(subscription.tenant_id, subscription.id)

    # -- routing a request to the right tenant -----------------------------

    def token_source(self, subscription: str) -> tuple[str, str | None]:
        """``(token cache key, the subscription to mint it from)``.

        A subscription ``az`` knows about is keyed by its tenant, so every
        subscription in that tenant shares one token. One it does not know --
        passed with ``--subscription`` before it has ever been logged into --
        is keyed on its own, and ``az`` is left to work the tenant out.
        """
        tenant = self._tenant_of.get(subscription.lower())
        if tenant:
            return tenant, self._representative.get(tenant, subscription)
        return f"subscription:{subscription.lower()}", subscription

    def tenant_of(self, subscription: str) -> str:
        """The tenant a subscription sits in, or empty if ``az`` has not seen it."""
        return self._tenant_of.get(subscription.lower(), "")

    def prepare(self, subscriptions: Iterable[str]) -> None:
        """Fetch a token for every tenant in scope, before any worker exists.

        This is the single-threaded fetch the whole credential design rests
        on. A scan that spans two tenants needs two tokens, and minting the
        second one lazily from inside the pool would put two ``az`` processes
        on the MSAL cache at once.
        """
        for subscription in subscriptions:
            key, source = self.token_source(subscription)
            self._credential.warm(key, source)

    # -- raw requests ------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        version: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        subscription: str | None = None,
    ) -> dict[str, Any]:
        """One ARM call. ``path`` is a resource path or a full URL.

        A full URL is what ``nextLink`` hands back, and it already carries its
        own query string, so it is passed through untouched.

        The token is chosen by which subscription the call is against, read out
        of the path. Pass ``subscription`` for the calls whose path does not
        name one -- Resource Graph puts it in the body instead.
        """
        if path.startswith("http"):
            url = path
        else:
            params = {"api-version": version, **(query or {})}
            url = f"{ARM}{path}?{urllib.parse.urlencode(params)}"
        target = subscription or subscription_of(path)
        key = self.token_source(target)[0] if target else DEFAULT_TENANT
        return self._send(method, url, body, key)

    def _send(
        self, method: str, url: str, body: dict[str, Any] | None, tenant: str
    ) -> dict[str, Any]:
        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(MAX_ATTEMPTS):
            request = urllib.request.Request(url, data=payload, method=method.upper())
            request.add_header("Authorization", f"Bearer {self._credential.token(tenant)}")
            request.add_header("Accept", "application/json")
            if payload is not None:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                error = _arm_error(exc, url)
                if error.status in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS - 1:
                    # ARM and Resource Graph both throttle, and both say for
                    # how long. Honouring Retry-After is what keeps a scan of
                    # many subscriptions from making the throttling worse.
                    time.sleep(_retry_after(exc, attempt))
                    continue
                raise error from exc
            except urllib.error.URLError as exc:
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(2**attempt)
                    continue
                raise ArmError(0, "network", str(exc.reason), url) from exc
        raise ArmError(0, "network", "exhausted retries", url)

    # -- listing -----------------------------------------------------------

    def list(
        self,
        path: str,
        resource_type: str,
        key: str = "value",
        subscription: str | None = None,
        **query: str,
    ) -> Iterator[dict[str, Any]]:
        """Every item of a paginated ARM list call.

        ARM pages uniformly: a response carries ``value`` and, when there is
        more, a ``nextLink`` holding a complete URL. That is the shape every
        list call takes regardless of provider, which is why this is one
        function and not one per service.
        """
        version = api_version(resource_type)
        # A nextLink is a full URL and still names its subscription, so the
        # tenant is derived per page as well -- but the first path is the
        # authority, and passing it on keeps a page-2 URL that has been
        # rewritten from breaking the routing.
        target = subscription or subscription_of(path)
        url: str | None = path
        first = True
        while url is not None:
            response = self.request(
                "GET", url, version, query=query if first else None, subscription=target
            )
            first = False
            yield from response.get(key) or []
            url = response.get("nextLink") or response.get("@odata.nextLink")

    def get(self, path: str, resource_type: str, **query: str) -> dict[str, Any]:
        """One ARM resource."""
        return self.request("GET", path, api_version(resource_type), query=query)

    # -- Resource Graph ----------------------------------------------------

    def graph(self, query: str, subscription: str) -> Iterator[dict[str, Any]]:
        """Every row of a Resource Graph query against one subscription.

        Resource Graph is Azure's answer to fanning out: one KQL query reads
        the cached ARM inventory for every region and every resource group at
        once, so a check that would otherwise be a call per resource group is
        a single call. Rows come back as plain dicts of whatever the query
        projected.

        Paging is by ``$skipToken`` rather than by a next URL, so it cannot go
        through ``list``.
        """
        version = api_version("Microsoft.ResourceGraph/resources")
        skip_token: str | None = None
        while True:
            options: dict[str, Any] = {"$top": GRAPH_PAGE, "resultFormat": "objectArray"}
            if skip_token:
                options["$skipToken"] = skip_token
            body = {"subscriptions": [subscription], "query": query, "options": options}
            # Resource Graph's path names no subscription -- it is in the body
            # -- so the tenant has to be named rather than derived.
            response = self.request(
                "POST",
                "/providers/Microsoft.ResourceGraph/resources",
                version,
                body=body,
                subscription=subscription,
            )
            rows = response.get("data") or []
            yield from rows
            skip_token = response.get("$skipToken")
            if not skip_token or not rows:
                return

    # -- provider registration --------------------------------------------

    def registered_providers(self, subscription: str) -> frozenset[str]:
        """Which resource provider namespaces are registered on a subscription.

        **This is not an optimisation.** ARM answers a list call against an
        unregistered provider with HTTP 200 and an empty page, so a check that
        simply ran would find nothing and the subscription would be reported
        clean. Asking first is the only way to tell "no waste" from "this
        service was never switched on here".

        Namespaces are returned lowercased. ARM treats them case-insensitively
        and does not report them in one casing: it lists ``microsoft.insights``
        where a check declares ``Microsoft.Insights``, and an exact match
        skips that check as unregistered.

        Cached per subscription: one call answers for every check.
        """
        with self._providers_lock:
            cached = self._providers.get(subscription)
            if cached is not None:
                return cached

        registered = {
            provider["namespace"].lower()
            for provider in self.list(
                f"/subscriptions/{subscription}/providers",
                "Microsoft.Resources/providers",
                **{"$select": "namespace,registrationState"},
            )
            if provider.get("registrationState") == "Registered" and provider.get("namespace")
        }
        result = frozenset(registered)
        with self._providers_lock:
            self._providers[subscription] = result
        return result


def _arm_error(exc: urllib.error.HTTPError, url: str) -> ArmError:
    """Turn an HTTPError into the code and message ARM actually sent.

    ARM nests its error as ``{"error": {"code": ..., "message": ...}}``, and
    the code is what tells a permission problem from a missing subscription.
    """
    code, message = "", ""
    try:
        body = json.loads(exc.read() or b"{}")
        detail = body.get("error") or body
        code = str(detail.get("code") or "")
        message = str(detail.get("message") or "")
    except Exception:  # noqa: BLE001 - an unparseable body still has a status
        pass
    return ArmError(exc.code, code or str(exc.code), message or exc.reason or "", url)


def _retry_after(exc: urllib.error.HTTPError, attempt: int) -> float:
    """How long ARM asked us to wait, or an exponential backoff if it did not."""
    header = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(float(header), 60.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(2**attempt)


# --------------------------------------------------------------------------
# Classifying failures
# --------------------------------------------------------------------------

# Error codes that mean "this subscription does not have that service", which
# is a fact about the subscription rather than a bug. A subscription that has
# never used App Service has no App Service waste.
_UNREGISTERED_CODES = frozenset(
    {"MissingSubscriptionRegistration", "NoRegisteredProviderFound", "InvalidResourceNamespace"}
)
_MISSING_CODES = frozenset({"SubscriptionNotFound", "ResourceGroupNotFound", "ResourceNotFound"})


def classify(error: Exception) -> str | None:
    """Name the benign failures, or None if the error is real.

    Returns ``"unregistered"`` for a provider that is not switched on,
    ``"forbidden"`` for a subscription the caller cannot read, and
    ``"missing"`` for one that is not there. All three are facts about the
    subscription, and a scan that aborted on any of them would be useless on
    any tenant that does not use every Azure service.
    """
    if isinstance(error, ProviderNotRegistered):
        return "unregistered"
    if not isinstance(error, ArmError):
        return None
    if error.code in _UNREGISTERED_CODES:
        return "unregistered"
    if error.code in _MISSING_CODES:
        return "missing"
    if error.status == 403:
        return "forbidden"
    if error.status == 404:
        return "missing"
    return None


def message_of(error: Exception) -> str:
    """The human-readable half of an ARM error, without the request URL."""
    if isinstance(error, ArmError):
        return error.message or error.code
    return str(error)


# --------------------------------------------------------------------------
# Resource ids
# --------------------------------------------------------------------------
#
# Every Azure resource is addressed by one long path:
#
#   /subscriptions/<sub>/resourceGroups/<rg>/providers/<namespace>/<type>/<name>
#
# It is the only identifier that is unique across a tenant, and it carries the
# subscription, the resource group and the name that every `az` command needs.
# So findings keep the whole id and pull the parts out of it rather than
# threading three fields through every call.


# Every ARM path that acts on a subscription names it as the second segment,
# whether it is a relative path or the full URL a nextLink hands back. That is
# what lets a request pick its own tenant without the caller threading one
# through every layer.
_SUBSCRIPTION_IN_PATH = re.compile(r"/subscriptions/([^/?#]+)", re.IGNORECASE)


def subscription_of(path: str) -> str:
    """The subscription an ARM path or URL acts on, or empty if it names none.

    ``/providers/Microsoft.ResourceGraph/resources`` names none: Resource
    Graph takes its subscriptions in the body, so callers of that one say
    which tenant they mean.
    """
    found = _SUBSCRIPTION_IN_PATH.search(path or "")
    return found.group(1) if found else ""


def _segments(resource_id: str) -> dict[str, str]:
    """The ``/key/value`` pairs of an ARM id, lowercased keys."""
    parts = [p for p in (resource_id or "").split("/") if p]
    return {parts[i].lower(): parts[i + 1] for i in range(0, len(parts) - 1, 2)}


def resource_group_of(resource_id: str) -> str:
    return _segments(resource_id).get("resourcegroups", "")


def name_of(resource_id: str) -> str:
    """The final segment of an ARM id, which is the resource's own name."""
    if not resource_id:
        return ""
    return resource_id.rstrip("/").rsplit("/", 1)[-1]


def type_of(resource_id: str) -> str:
    """The ``Namespace/type`` of an ARM id, as ARM itself spells it.

    Lowercased, because Resource Graph returns types lowercased and ARM does
    not, and a comparison that depends on which one answered is a bug waiting
    for the other to answer.
    """
    parts = [p for p in (resource_id or "").split("/") if p]
    try:
        index = [p.lower() for p in parts].index("providers")
    except ValueError:
        return ""
    tail = parts[index + 1 :]
    if len(tail) < 2:
        return ""
    # namespace, then every other segment: type/name/subtype/name...
    pieces = [tail[0]] + tail[1::2]
    return "/".join(pieces).lower()


def namespace_of(resource_type: str) -> str:
    """``"Microsoft.Compute/disks"`` -> ``"Microsoft.Compute"``."""
    return resource_type.split("/", 1)[0]


def region_of(location: str) -> str:
    """The region a location prices in.

    Azure resources are regional, not zonal: an availability zone is a
    placement constraint inside a region rather than a separate location, and
    nothing is billed differently for being in zone 2. So a location already
    is its own region, and this exists to give pricing lookups one shape
    across clouds and to normalise the spelling -- ARM answers ``eastus``
    where a human writes ``East US``.
    """
    if not location:
        return GLOBAL
    return location.replace(" ", "").lower()
