"""Tests for `rtbf_agent.plugins.discovery.gmail` (spec S3.1, issue #13).

Covers: `GmailDiscoverySource.discover` against a fixture inbox (a fake `GmailClient`, no live
Gmail account or network call, ever); the exact read-only OAuth scope requested; and the
`googleapiclient`/`google_auth_oauthlib` adapter boundary (`GoogleApiGmailClient`,
`build_gmail_client`, `build_installed_app_flow`), exercised against fakes standing in for those
optional SDKs so this test file runs even when they aren't installed.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping, Sequence

import pytest

from rtbf_agent.core.interfaces import DiscoveredAccount, DiscoverySource
from rtbf_agent.plugins.discovery.gmail import (
    GMAIL_READONLY_SCOPE,
    GmailClient,
    GmailDiscoverySource,
    GoogleApiGmailClient,
    build_gmail_client,
    build_installed_app_flow,
)

TARGET_EMAIL = "user@example.invalid"

# --------------------------------------------------------------------------------------------- #
# R21 -- GmailDiscoverySource is a real DiscoverySource implementation, added without touching
# core/interfaces.py.
# --------------------------------------------------------------------------------------------- #


def test_gmail_discovery_source_is_a_discovery_source() -> None:
    assert issubclass(GmailDiscoverySource, DiscoverySource)


# --------------------------------------------------------------------------------------------- #
# The OAuth scope requested must be exactly Gmail's readonly scope -- never broader. This is the
# regression guard named explicitly in issue #13's verification checklist.
# --------------------------------------------------------------------------------------------- #


def test_oauth_scope_is_exactly_gmail_readonly() -> None:
    assert GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"


def test_oauth_scope_is_not_a_write_or_send_scope() -> None:
    forbidden = {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://mail.google.com/",
    }
    assert GMAIL_READONLY_SCOPE not in forbidden


def test_build_installed_app_flow_requests_only_the_readonly_scope(monkeypatch) -> None:
    """`build_installed_app_flow` must pass `[GMAIL_READONLY_SCOPE]` and nothing else."""
    captured: dict[str, object] = {}

    class _FakeFlow:
        pass

    def _from_client_secrets_file(client_secrets_file, scopes):  # noqa: ANN001
        captured["client_secrets_file"] = client_secrets_file
        captured["scopes"] = scopes
        return _FakeFlow()

    fake_flow_module = types.SimpleNamespace(
        InstalledAppFlow=types.SimpleNamespace(from_client_secrets_file=_from_client_secrets_file)
    )
    fake_oauthlib_module = types.ModuleType("google_auth_oauthlib")
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", fake_oauthlib_module)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", fake_flow_module)

    result = build_installed_app_flow("client_secret.json")

    assert isinstance(result, _FakeFlow)
    assert captured["client_secrets_file"] == "client_secret.json"
    assert captured["scopes"] == [GMAIL_READONLY_SCOPE]
    assert captured["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]


# --------------------------------------------------------------------------------------------- #
# GmailDiscoverySource.discover against a fixture inbox -- the fake GmailClient below is the
# integration-test seam: it satisfies the GmailClient Protocol without any real Gmail API access.
# --------------------------------------------------------------------------------------------- #


class _FixtureGmailClient:
    """A fake `GmailClient` backed by a fixed, in-memory (recorded/synthetic) inbox."""

    def __init__(self, messages: Mapping[str, Mapping[str, str]]) -> None:
        self._messages = messages
        self.queries: list[str] = []

    def list_message_ids(self, query: str) -> Sequence[str]:
        self.queries.append(query)
        return list(self._messages.keys())

    def get_message_headers(self, message_id: str) -> Mapping[str, str]:
        return self._messages[message_id]


_FIXTURE_INBOX: dict[str, dict[str, str]] = {
    "msg-welcome-notion": {
        "Subject": "Welcome to Notion!",
        "From": "Notion <team@notion.so>",
    },
    "msg-confirm-github": {
        "Subject": "Please confirm your email address",
        "From": "GitHub <noreply@github.com>",
    },
    "msg-reset-spotify": {
        "Subject": "Reset your password",
        "From": "Spotify <no-reply@spotify.com>",
    },
    "msg-newsletter-example": {
        "Subject": "Your weekly digest is here",
        "From": "Example News <digest@example.invalid>",
    },
    "msg-second-notion-email": {
        # Same domain as msg-welcome-notion -- should not produce a second DiscoveredAccount.
        "Subject": "Your account has been created",
        "From": "Notion <team@notion.so>",
    },
    "msg-malformed-sender": {
        "Subject": "Verify your account",
        "From": "not-an-email-address",
    },
}


@pytest.fixture()
def fixture_client() -> _FixtureGmailClient:
    return _FixtureGmailClient(_FIXTURE_INBOX)


def test_gmail_client_protocol_is_satisfied_by_the_fixture(
    fixture_client: _FixtureGmailClient,
) -> None:
    # Structural check: GmailClient is a Protocol, so this is what "implements it" means here.
    assert isinstance(fixture_client, GmailClient)


async def test_discover_finds_matching_accounts_and_ignores_non_matching_mail(
    fixture_client: _FixtureGmailClient,
) -> None:
    source = GmailDiscoverySource(fixture_client)

    accounts = await source.discover(TARGET_EMAIL)

    domains = {account.domain for account in accounts}
    assert domains == {"notion.so", "github.com", "spotify.com"}
    # The newsletter and the malformed-sender message never match a discovery pattern/site.
    assert "example.invalid" not in domains


async def test_discover_populates_discovery_evidence_fields(
    fixture_client: _FixtureGmailClient,
) -> None:
    source = GmailDiscoverySource(fixture_client)

    accounts = await source.discover(TARGET_EMAIL)
    by_domain = {account.domain: account for account in accounts}

    github = by_domain["github.com"]
    assert isinstance(github, DiscoveredAccount)
    assert github.site == "GitHub"
    assert github.target_email == TARGET_EMAIL
    assert github.evidence["email_id"] == "msg-confirm-github"
    assert github.evidence["matched_pattern"] == "account_confirmation"
    assert github.evidence["matched_header"] == "Subject"


async def test_discover_deduplicates_repeated_matches_from_the_same_domain(
    fixture_client: _FixtureGmailClient,
) -> None:
    source = GmailDiscoverySource(fixture_client)

    accounts = await source.discover(TARGET_EMAIL)

    notion_accounts = [a for a in accounts if a.domain == "notion.so"]
    assert len(notion_accounts) == 1
    # First match wins -- the welcome email, not the later account-created email.
    assert notion_accounts[0].evidence["email_id"] == "msg-welcome-notion"


async def test_discover_on_empty_inbox_returns_empty_sequence_without_raising() -> None:
    source = GmailDiscoverySource(_FixtureGmailClient({}))

    accounts = await source.discover(TARGET_EMAIL)

    assert accounts == ()


async def test_discover_uses_the_configured_query(fixture_client: _FixtureGmailClient) -> None:
    source = GmailDiscoverySource(fixture_client, query="category:primary newer_than:30d")

    await source.discover(TARGET_EMAIL)

    assert fixture_client.queries == ["category:primary newer_than:30d"]


async def test_discover_respects_max_messages(fixture_client: _FixtureGmailClient) -> None:
    source = GmailDiscoverySource(fixture_client, max_messages=1)

    accounts = await source.discover(TARGET_EMAIL)

    # Only the first message id in the fixture inbox is inspected -- Python dicts preserve
    # insertion order, so this is deterministic.
    assert len(accounts) == 1
    assert accounts[0].domain == "notion.so"


async def test_concurrent_discover_calls_never_overlap_on_the_same_client() -> None:
    """`googleapiclient` resources aren't documented thread-safe -- concurrent `discover` calls on
    the same `GmailDiscoverySource` must serialize, not race, on the shared `GmailClient`.
    """
    import asyncio
    import threading
    import time

    in_flight = 0
    max_observed_in_flight = 0
    tracking_lock = threading.Lock()

    class _ConcurrencyTrackingClient:
        def list_message_ids(self, query: str) -> Sequence[str]:
            nonlocal in_flight, max_observed_in_flight
            with tracking_lock:
                in_flight += 1
                max_observed_in_flight = max(max_observed_in_flight, in_flight)
            try:
                time.sleep(0.01)
                return []
            finally:
                with tracking_lock:
                    in_flight -= 1

        def get_message_headers(self, message_id: str) -> Mapping[str, str]:
            return {}

    source = GmailDiscoverySource(_ConcurrencyTrackingClient())

    await asyncio.gather(*(source.discover(TARGET_EMAIL) for _ in range(5)))

    assert max_observed_in_flight == 1


# --------------------------------------------------------------------------------------------- #
# GoogleApiGmailClient -- the adapter translating GmailClient calls into the real googleapiclient
# chained-call shape. Exercised against a fake Resource mimicking that shape, never a real one.
# --------------------------------------------------------------------------------------------- #


class _FakeExecuteResult:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


class _FakeMessages:
    def __init__(self, list_response: dict, get_responses: dict[str, dict]) -> None:
        self._list_response = list_response
        self._get_responses = get_responses
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def list(self, userId: str, q: str) -> _FakeExecuteResult:  # noqa: N803
        self.list_calls.append({"userId": userId, "q": q})
        return _FakeExecuteResult(self._list_response)

    def get(self, userId: str, id: str, format: str, metadataHeaders: list[str]):  # noqa: A002, N803
        self.get_calls.append(
            {"userId": userId, "id": id, "format": format, "metadataHeaders": metadataHeaders}
        )
        return _FakeExecuteResult(self._get_responses[id])


class _FakeUsers:
    def __init__(self, messages: _FakeMessages) -> None:
        self._messages = messages

    def messages(self) -> _FakeMessages:
        return self._messages


class _FakeGmailResource:
    def __init__(self, list_response: dict, get_responses: dict[str, dict]) -> None:
        self.messages = _FakeMessages(list_response, get_responses)

    def users(self) -> _FakeUsers:
        return _FakeUsers(self.messages)


def test_google_api_gmail_client_list_message_ids_translates_response() -> None:
    resource = _FakeGmailResource(
        list_response={"messages": [{"id": "m1"}, {"id": "m2"}]},
        get_responses={},
    )
    client = GoogleApiGmailClient(resource)

    ids = client.list_message_ids("category:primary")

    assert ids == ["m1", "m2"]
    assert resource.messages.list_calls == [{"userId": "me", "q": "category:primary"}]


def test_google_api_gmail_client_get_message_headers_translates_response() -> None:
    resource = _FakeGmailResource(
        list_response={},
        get_responses={
            "m1": {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Welcome!"},
                        {"name": "From", "value": "Site <team@site.example>"},
                    ]
                }
            }
        },
    )
    client = GoogleApiGmailClient(resource)

    headers = client.get_message_headers("m1")

    assert headers == {"Subject": "Welcome!", "From": "Site <team@site.example>"}
    assert resource.messages.get_calls == [
        {
            "userId": "me",
            "id": "m1",
            "format": "metadata",
            "metadataHeaders": ["Subject", "From"],
        }
    ]


def test_google_api_gmail_client_satisfies_gmail_client_protocol() -> None:
    client = GoogleApiGmailClient(_FakeGmailResource(list_response={}, get_responses={}))
    assert isinstance(client, GmailClient)


async def test_gmail_discovery_source_works_against_google_api_gmail_client() -> None:
    """End-to-end: GmailDiscoverySource driven by the real adapter, over a fake Resource."""
    resource = _FakeGmailResource(
        list_response={"messages": [{"id": "m1"}]},
        get_responses={
            "m1": {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Verify your email"},
                        {"name": "From", "value": "Acme <hello@acme.example>"},
                    ]
                }
            }
        },
    )
    client = GoogleApiGmailClient(resource)
    source = GmailDiscoverySource(client)

    accounts = await source.discover(TARGET_EMAIL)

    assert len(accounts) == 1
    assert accounts[0].domain == "acme.example"
    assert accounts[0].evidence["matched_pattern"] == "verify_email"


def test_build_gmail_client_wires_credentials_through_lazily_imported_build(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel_resource = object()

    def _fake_build(serviceName, version, credentials):  # noqa: ANN001, N803
        captured["serviceName"] = serviceName
        captured["version"] = version
        captured["credentials"] = credentials
        return sentinel_resource

    fake_discovery_module = types.SimpleNamespace(build=_fake_build)
    fake_googleapiclient = types.ModuleType("googleapiclient")
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_googleapiclient)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_discovery_module)

    fake_credentials = object()
    client = build_gmail_client(fake_credentials)

    assert isinstance(client, GoogleApiGmailClient)
    assert captured == {
        "serviceName": "gmail",
        "version": "v1",
        "credentials": fake_credentials,
    }


def test_build_gmail_client_rejects_credentials_missing_the_readonly_scope(monkeypatch) -> None:
    """Defense-in-depth: credentials that expose `.scopes` but don't include the readonly scope
    (e.g. accidentally requested with `gmail.modify`) must be rejected before ever reaching
    `googleapiclient.discovery.build`.
    """
    fake_discovery_module = types.SimpleNamespace(build=lambda *a, **k: object())
    fake_googleapiclient = types.ModuleType("googleapiclient")
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_googleapiclient)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_discovery_module)

    over_scoped_credentials = types.SimpleNamespace(
        scopes=["https://www.googleapis.com/auth/gmail.modify"]
    )

    with pytest.raises(ValueError, match="gmail.readonly"):
        build_gmail_client(over_scoped_credentials)


def test_build_gmail_client_accepts_credentials_scoped_to_readonly(monkeypatch) -> None:
    fake_discovery_module = types.SimpleNamespace(build=lambda *a, **k: object())
    fake_googleapiclient = types.ModuleType("googleapiclient")
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_googleapiclient)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_discovery_module)

    correctly_scoped_credentials = types.SimpleNamespace(scopes=[GMAIL_READONLY_SCOPE])

    client = build_gmail_client(correctly_scoped_credentials)

    assert isinstance(client, GoogleApiGmailClient)


def test_build_gmail_client_skips_scope_check_when_credentials_expose_no_scopes(
    monkeypatch,
) -> None:
    """Test doubles (and some real credential objects) may not expose `.scopes` at all -- the
    check must be best-effort, not required, so it can't break legitimate callers.
    """
    fake_discovery_module = types.SimpleNamespace(build=lambda *a, **k: object())
    fake_googleapiclient = types.ModuleType("googleapiclient")
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_googleapiclient)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_discovery_module)

    client = build_gmail_client(object())

    assert isinstance(client, GoogleApiGmailClient)
