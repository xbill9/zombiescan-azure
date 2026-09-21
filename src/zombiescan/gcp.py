"""The Google Cloud client layer.

One discovery-built client covers every API a check needs, so adding coverage
for a new service is a new entry in ``API_VERSIONS`` rather than a new
dependency. That uniformity is what lets ``building.simple_check`` and the
``Step`` runner in ``clean.py`` drive any service without knowing which one
they are talking to.

Two facts about Google's APIs shape everything above this module:

* **Compute Engine has ``aggregatedList``.** One call returns disks (or
  addresses, instances, routers...) across every zone and region at once. So a
  check is scoped to a *project*, not to a region, and fanning out per zone --
  the way an AWS scanner must -- would turn one call into a hundred.
* **Most other APIs accept ``locations/-``**, a wildcard meaning every
  location. Filestore, KMS, Artifact Registry and Cloud Logging are all
  listable for a whole project in a single call.

Together those remove the region fan-out entirely. ``Finding.location`` still
records where each resource actually lives, because that is what the operator
needs to delete it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import google.auth
import google.auth.exceptions
import google_auth_httplib2
import googleapiclient.discovery
import googleapiclient.errors
import httplib2

# Which version of each API to build. Pinned rather than "latest" because a
# discovery document that changes shape under us would break checks silently.
API_VERSIONS = {
    "artifactregistry": "v1",
    "cloudbilling": "v1",
    "cloudkms": "v1",
    "cloudresourcemanager": "v3",
    "compute": "v1",
    "container": "v1",
    "dns": "v1",
    "file": "v1",
    "logging": "v2",
    "monitoring": "v3",
    "secretmanager": "v1",
    # Cloud SQL's GA surface is still published as v1beta4; v1 omits several
    # of the settings the stopped-instance check reads.
    "sqladmin": "v1beta4",
    "storage": "v1",
}

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

# Wildcard location. Every API listed here accepts it in a parent path and
# returns resources from every location at once.
ANY_LOCATION = "-"


class CredentialError(RuntimeError):
    """No usable credentials. The message names the fix."""


class ApiNotEnabled(RuntimeError):
    """The service is not enabled on this project.

    Not a failure: a project that has never used Filestore has no Filestore
    waste, and reporting an error per disabled API would bury the findings.
    """


def default_credentials(quota_project: str | None = None):
    """Application Default Credentials, or a message naming how to get them."""
    try:
        credentials, project = google.auth.default(scopes=SCOPES, quota_project_id=quota_project)
    except google.auth.exceptions.DefaultCredentialsError as exc:
        raise CredentialError(
            "No Google Cloud credentials found. Run 'gcloud auth application-default login' first."
        ) from exc
    return make_thread_safe(credentials), project


def make_thread_safe(credentials: Any) -> Any:
    """Serialise token refresh, and fetch the first token before any thread runs.

    ``Credentials.refresh`` is not safe to call concurrently. Every request the
    scanner makes goes through ``before_request``, which refreshes when the
    token is missing or expired -- so at the start of a scan every worker
    thread finds no token and calls refresh at the same moment. The refresh
    signs a JWT through OpenSSL via cffi, and running that concurrently on one
    credentials object corrupts the heap: the process dies with SIGSEGV or a
    glibc abort rather than an exception, which is about as bad as a failure
    mode gets.

    Two things fix it, and both are needed. The token is fetched once here,
    single-threaded, so the common case never races. And refresh is wrapped in
    a lock that re-checks validity, so an expiry part-way through a long scan
    refreshes exactly once instead of once per waiting thread.
    """
    lock = threading.Lock()
    original = credentials.refresh

    def locked_refresh(request: Any) -> None:
        with lock:
            # Another thread may have refreshed while this one waited.
            if not credentials.valid:
                original(request)

    credentials.refresh = locked_refresh

    try:
        credentials.refresh(google_auth_httplib2.Request(httplib2.Http()))
    except Exception as exc:  # noqa: BLE001 - surfaced as a credential error below
        raise CredentialError(
            f"Google Cloud credentials could not be refreshed ({type(exc).__name__}). "
            "Run 'gcloud auth application-default login' again."
        ) from exc
    return credentials


class Clients:
    """Discovery clients for one set of credentials, one set per thread.

    **A discovery client cannot be shared between threads.** The ``httplib2``
    connection underneath it is not thread-safe, and the engine runs every
    check concurrently: sharing one ``compute`` client across that pool
    corrupts the heap rather than raising, which is about as bad as a failure
    mode gets. So clients are held in thread-local storage and each worker
    builds its own.

    That is only affordable because the clients are built from the discovery
    documents bundled with ``google-api-python-client`` rather than fetched.
    Building every API this way takes milliseconds and no network at all, so a
    per-thread copy costs nothing. It also pins the API surface to the
    installed library version, which means a scan's behaviour changes when the
    dependency is upgraded and not before.
    """

    def __init__(self, credentials: Any) -> None:
        self._credentials = credentials
        self._local = threading.local()

    def get(self, api: str) -> Any:
        version = API_VERSIONS.get(api)
        if version is None:
            known = ", ".join(sorted(API_VERSIONS))
            raise KeyError(f"unknown API {api!r}. Add it to gcp.API_VERSIONS. Known: {known}")

        built = getattr(self._local, "built", None)
        if built is None:
            built = self._local.built = {}
        if api not in built:
            built[api] = self._build(api, version)
        return built[api]

    def _build(self, api: str, version: str) -> Any:
        common = {
            "credentials": self._credentials,
            # The on-disk discovery cache needs oauth2client, which is not a
            # dependency and is deprecated anyway.
            "cache_discovery": False,
        }
        try:
            return googleapiclient.discovery.build(api, version, static_discovery=True, **common)
        except googleapiclient.errors.UnknownApiNameOrVersion:
            # An API added to API_VERSIONS before the installed library ships
            # a document for it. Fetching one works, it just costs a request
            # per thread.
            return googleapiclient.discovery.build(api, version, static_discovery=False, **common)


def collection(client: Any, path: str) -> Any:
    """Walk a dotted resource path to its collection object.

    ``collection(compute, "disks")`` is ``compute.disks()``;
    ``collection(secretmanager, "projects.secrets")`` is
    ``secretmanager.projects().secrets()``. Checks and cleaners both name
    operations this way, so a cleaner's ``Step`` is executable data rather
    than a closure.
    """
    node = client
    for part in path.split("."):
        node = getattr(node, part)()
    return node


def call(client: Any, operation: str, **params: Any) -> Any:
    """Execute one API method named as a dotted path, e.g. ``disks.delete``."""
    path, _, method = operation.rpartition(".")
    target = collection(client, path) if path else client
    return getattr(target, method)(**params).execute()


def classify(error: Exception) -> str | None:
    """Name the benign HTTP failures, or None if the error is real.

    Returns ``"disabled"`` for an API that is switched off on the project and
    ``"forbidden"`` for one the caller cannot read. Both are facts about the
    project rather than bugs, and a scan that aborted on either would be
    useless on any project that does not use every Google service.
    """
    if not isinstance(error, googleapiclient.errors.HttpError):
        return None
    status = error.resp.status if error.resp is not None else 0
    if status == 403:
        text = str(error)
        if "has not been used in project" in text or "it is disabled" in text:
            return "disabled"
        if "SERVICE_DISABLED" in text or "accessNotConfigured" in text:
            return "disabled"
        return "forbidden"
    if status == 404:
        return "missing"
    return None


def message_of(error: googleapiclient.errors.HttpError) -> str:
    """The human-readable half of an HttpError, without the request URL."""
    try:
        detail = error.error_details  # type: ignore[attr-defined]
        if detail:
            return str(detail)
    except Exception:  # noqa: BLE001 - error_details is best-effort
        pass
    reason = getattr(error, "reason", None)
    return str(reason) if reason else str(error)


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def paginate(
    client: Any, path: str, method: str = "list", key: str = "items", **params: Any
) -> Iterator[dict[str, Any]]:
    """Every item from a paginated list call.

    The discovery client pages with a ``<method>_next`` companion that returns
    None when there is nothing more, so this is the shape every Google list
    call takes regardless of service.
    """
    target = collection(client, path)
    request = getattr(target, method)(**params)
    next_method = getattr(target, f"{method}_next", None)
    while request is not None:
        response = request.execute()
        yield from response.get(key, []) or []
        if next_method is None:
            return
        request = next_method(request, response)


def aggregated(
    client: Any, path: str, key: str, **params: Any
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every item of an ``aggregatedList``, paired with the scope it came from.

    Yields ``("zones/us-central1-a", item)`` or ``("regions/us-central1", item)``
    or ``("global", item)``. The scope is the only place the response says
    where a resource lives -- many Compute items carry a ``zone`` field, but
    global ones carry nothing at all.

    Scopes holding no items carry a ``warning`` instead, which is how Compute
    reports "nothing here"; those are skipped rather than surfaced.
    """
    target = collection(client, path)
    request = target.aggregatedList(**params)
    while request is not None:
        response = request.execute()
        for scope, scoped in (response.get("items") or {}).items():
            for item in scoped.get(key, []) or []:
                yield scope, item
        request = target.aggregatedList_next(request, response)


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

GLOBAL = "global"


def location_from_scope(scope: str) -> str:
    """``"zones/us-central1-a"`` -> ``"us-central1-a"``; ``"global"`` -> ``"global"``."""
    if "/" in scope:
        return scope.split("/", 1)[1]
    return scope


def last_segment(url: str | None) -> str:
    """The final path segment of a selfLink or partial resource URL.

    Google returns references as full URLs -- a disk's ``zone`` is
    ``https://.../projects/p/zones/us-central1-a`` -- and every check wants
    the bare name at the end.
    """
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]


def region_of(location: str) -> str:
    """The region a location belongs to.

    A zone name is its region plus a letter suffix, so ``us-central1-a``
    belongs to ``us-central1``. Regions and ``global`` are returned unchanged.
    Pricing is regional, so this is what a rate lookup is keyed by.
    """
    if not location or location == GLOBAL:
        return GLOBAL
    parts = location.rsplit("-", 1)
    # A zone's last segment is a single letter; a region's is a digit-bearing
    # word like "central1", so the split only applies to the former.
    if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha():
        return parts[0]
    return location


def label(resource: dict[str, Any], key: str = "name") -> str | None:
    """The value of one label, if the resource carries it.

    Labels are GCP's equivalent of AWS tags and are already a flat dict, so
    this is a lookup rather than a scan over key/value pairs.
    """
    return (resource.get("labels") or {}).get(key)
