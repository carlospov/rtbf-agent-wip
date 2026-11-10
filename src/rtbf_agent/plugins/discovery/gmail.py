"""Gmail inbox `DiscoverySource` (spec S3.1) -- the first real, non-mock discovery plugin.

Scans the user's own Gmail inbox for account-signup/confirmation/password-recovery patterns and
turns matches into `DiscoveredAccount`s, each carrying machine-checkable evidence that populates
`Case.discovery_evidence` (spec S3.2). Breach-database lookups (e.g. Have I Been Pwned) are
explicitly optional per spec S3.1 and out of scope for this module.

Enforces:
- **R21** -- implements `DiscoverySource` from `core/interfaces.py` with no change to that file,
  proving a real discovery source can be added purely as a plugin under `plugins/`.
- Read-only by construction -- see `GMAIL_READONLY_SCOPE` below, the only OAuth scope this plugin
  ever requests. There is no code path in this module that calls a Gmail *write* endpoint (send,
  modify, trash, delete): `GmailClient` only exposes `list_message_ids`/`get_message_headers`.

Design: `GmailDiscoverySource` depends on the narrow `GmailClient` Protocol below, not on
`googleapiclient` directly. `GoogleApiGmailClient` is the only concrete implementation of that
Protocol and the only place in this module that imports `googleapiclient` -- tests exercise
`GmailDiscoverySource` against a fake `GmailClient`, never a real Gmail account or network call.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from email.utils import parseaddr
from typing import Any, Protocol, runtime_checkable

from rtbf_agent.core.interfaces import DiscoveredAccount, DiscoverySource

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
"""The only Gmail OAuth scope this plugin ever requests.

Deliberately the narrowest read-only scope Gmail offers -- never `gmail.modify`, `gmail.send`, or
the full-account `mail.google.com` scope. `test_gmail.py` asserts this constant's exact value so a
future edit can't silently widen it to a scope that could send or delete mail.
"""

# Gmail search query restricting the scan to the primary inbox category, which is where signup /
# confirmation / password-recovery mail typically lands (as opposed to Promotions/Social). Callers
# can override this via `GmailDiscoverySource(..., query=...)` if their inbox is organized
# differently.
_DEFAULT_QUERY = "category:primary"

# (pattern name, subject-line regex) pairs identifying account-lifecycle emails (spec S3.1:
# "patrones de correos de bienvenida/confirmación/recuperación de contraseña"). Checked in order;
# the first match wins.
_SUBJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("welcome_email", re.compile(r"\bwelcome\s+to\b|\bwelcome\b.{0,20}\baboard\b", re.IGNORECASE)),
    (
        "account_confirmation",
        re.compile(r"\bconfirm(?:ed|ation)?\s+your\s+(?:account|email|address)\b", re.IGNORECASE),
    ),
    ("verify_email", re.compile(r"\bverify\s+your\s+(?:email|account)\b", re.IGNORECASE)),
    (
        "password_recovery",
        re.compile(r"\b(?:reset|recover|change)\s+your\s+password\b", re.IGNORECASE),
    ),
    (
        "account_created",
        re.compile(r"\byour\s+account\s+(?:has\s+been\s+)?(?:created|set\s*up)\b", re.IGNORECASE),
    ),
)


def _match_subject(subject: str) -> str | None:
    """Return the name of the first pattern matching `subject`, or `None`."""
    for name, pattern in _SUBJECT_PATTERNS:
        if pattern.search(subject):
            return name
    return None


def _parse_sender(from_header: str) -> tuple[str, str] | None:
    """Split a `From` header into `(site_name, domain)`, or `None` if it carries no address."""
    display_name, address = parseaddr(from_header)
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return None
    site = display_name.strip() or domain
    return site, domain


@runtime_checkable
class GmailClient(Protocol):
    """The narrow Gmail-API surface `GmailDiscoverySource` depends on -- read-only, two methods.

    This is the seam tests use to avoid a live Gmail account or network call: any object providing
    these two methods (a fixture-backed fake, in tests) satisfies the Protocol without needing
    `googleapiclient` installed or a real OAuth token. `GoogleApiGmailClient` below is the only
    production implementation.
    """

    def list_message_ids(self, query: str) -> Sequence[str]:
        """Return the ids of messages matching `query` (a Gmail search query)."""
        ...

    def get_message_headers(self, message_id: str) -> Mapping[str, str]:
        """Return the headers (e.g. `Subject`, `From`) of the message with id `message_id`."""
        ...


class GmailDiscoverySource(DiscoverySource):
    """Scans the user's Gmail inbox for account-signup/confirmation/recovery patterns.

    Enforces:
    - **R21** -- a concrete `DiscoverySource` living entirely under `plugins/discovery/`; nothing
      in `core/interfaces.py` changed to accommodate it.

    Never mutates `Case`/`Event` state itself (that stays the orchestrator's job, per the
    `DiscoverySource.discover` contract) and never raises on "nothing found" -- an inbox with no
    matching mail simply yields an empty sequence.
    """

    def __init__(
        self,
        client: GmailClient,
        *,
        query: str = _DEFAULT_QUERY,
        max_messages: int = 50,
    ) -> None:
        self._client = client
        self._query = query
        self._max_messages = max_messages
        # `googleapiclient` resources (the production `GmailClient`) wrap an `httplib2.Http`
        # transport that is documented as *not* thread-safe when shared across threads. Each
        # `asyncio.to_thread` call below may land on a different worker thread, so if `discover`
        # is ever invoked concurrently (e.g. an orchestrator `asyncio.gather`-ing several target
        # emails against the same source instance), two threads could call into `self._client`
        # at once. This lock serializes `discover` calls on a given instance so that never
        # happens -- concurrent callers queue instead of racing on the shared client.
        self._lock = asyncio.Lock()

    async def discover(self, target_email: str) -> Sequence[DiscoveredAccount]:
        async with self._lock:
            message_ids = await asyncio.to_thread(self._client.list_message_ids, self._query)

            # Keyed by domain so a site that sent several matching emails (e.g. both a welcome
            # email and a later password reset) only produces one `DiscoveredAccount`, not one
            # per message.
            discovered: dict[str, DiscoveredAccount] = {}
            for message_id in message_ids[: self._max_messages]:
                headers = await asyncio.to_thread(self._client.get_message_headers, message_id)
                subject = headers.get("Subject", "")
                pattern_name = _match_subject(subject)
                if pattern_name is None:
                    continue

                parsed_sender = _parse_sender(headers.get("From", ""))
                if parsed_sender is None:
                    continue
                site, domain = parsed_sender
                if domain in discovered:
                    continue

                discovered[domain] = DiscoveredAccount(
                    site=site,
                    domain=domain,
                    target_email=target_email,
                    evidence={
                        "email_id": message_id,
                        "matched_pattern": pattern_name,
                        "matched_header": "Subject",
                    },
                )

            return tuple(discovered.values())


class GoogleApiGmailClient:
    """`GmailClient` backed by a real `googleapiclient` Gmail API resource.

    The only place in this module that touches `googleapiclient` -- constructed from OAuth2
    credentials that must already be scoped to `GMAIL_READONLY_SCOPE` (see
    `build_installed_app_flow`). Every method here is a `list`/`get` read against the Gmail API;
    none can send, modify, or delete mail, matching the read-only scope this plugin requests.
    """

    def __init__(self, resource: Any) -> None:
        self._resource = resource

    def list_message_ids(self, query: str) -> Sequence[str]:
        # Deliberately reads only the first page of results (no `nextPageToken` follow-up): Gmail
        # returns its most recent matches first, and `GmailDiscoverySource` already caps how many
        # it inspects via `max_messages`, so a very large inbox is bounded rather than fully
        # paginated. A follow-up source can add pagination without touching `GmailDiscoverySource`
        # or the `GmailClient` Protocol -- only this method's implementation would change.
        response = self._resource.users().messages().list(userId="me", q=query).execute()
        return [message["id"] for message in response.get("messages", [])]

    def get_message_headers(self, message_id: str) -> Mapping[str, str]:
        message = (
            self._resource.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From"],
            )
            .execute()
        )
        headers = message.get("payload", {}).get("headers", [])
        return {header["name"]: header["value"] for header in headers}


def build_gmail_client(credentials: Any) -> GoogleApiGmailClient:
    """Wrap already-authorized `credentials` in a `GoogleApiGmailClient`.

    `credentials` must already be scoped to exactly `GMAIL_READONLY_SCOPE` (e.g. obtained via
    `build_installed_app_flow`) -- this function performs no OAuth handshake and does not itself
    complete an authorization flow; it only wires already-authorized credentials to the Gmail API
    client. Imports `googleapiclient` lazily so the rest of this module (and anything that only
    needs `GmailDiscoverySource` against a fake `GmailClient` in tests) never requires it installed.

    As defense-in-depth against a caller accidentally passing broader-than-readonly credentials
    (e.g. `gmail.modify` or the full-account `mail.google.com` scope), this raises `ValueError` if
    `credentials` exposes a `scopes` attribute that doesn't include `GMAIL_READONLY_SCOPE`. This is
    best-effort, not a guarantee: `credentials` is typed `Any` because callers may pass any
    already-authorized object (including test doubles) that may not expose `.scopes` at all, in
    which case this check is silently skipped -- it cannot replace requesting the right scope in
    the first place (see `build_installed_app_flow`).
    """
    scopes = getattr(credentials, "scopes", None)
    if scopes is not None and GMAIL_READONLY_SCOPE not in scopes:
        raise ValueError(
            f"credentials must be scoped to include {GMAIL_READONLY_SCOPE!r}, got {scopes!r}"
        )

    from googleapiclient.discovery import build

    resource = build("gmail", "v1", credentials=credentials)
    return GoogleApiGmailClient(resource)


def build_installed_app_flow(client_secrets_file: str) -> Any:
    """Build an OAuth `InstalledAppFlow` requesting exactly `[GMAIL_READONLY_SCOPE]`.

    Never requests a broader scope -- there is no parameter to widen it. Imports
    `google_auth_oauthlib` lazily for the same reason `build_gmail_client` imports
    `googleapiclient` lazily: this module stays importable (and `GmailDiscoverySource` testable)
    without either package installed.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow.from_client_secrets_file(
        client_secrets_file, scopes=[GMAIL_READONLY_SCOPE]
    )
