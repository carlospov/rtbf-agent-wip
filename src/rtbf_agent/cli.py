"""Runnable CLI entrypoint: discover -> case -> agent loop -> approval -> real send (issue #36).

**Since issue #82, the pipeline described below is no longer what runs by default.** Bare
`rtbf-agent`/`forget`/`python -m rtbf_agent` now opens `shell_main()`'s interactive shell -- see
that function's docstring. Since a later maintainer decision, the interactive shell is in fact the
*only* CLI surface at all: every standalone one-shot subcommand this module used to dispatch via
`argparse` is gone, and `main()` unconditionally opens the shell regardless of `argv`. The full
discover/draft/approve/send pass this module docstring otherwise describes is still exactly what
it always was, just reachable via the shell's `scan-inbox` command (`main_async()`, unchanged)
rather than being the implicit default.

**Since issue #86, Gmail discovery is no longer required to advance a case at all.** The
classify/draft/recipient-confirm/approval/send pass `run_once()` runs is independent of Gmail --
`scan-inbox` (`main_async()`, a shell command) still bundles real Gmail discovery + follow-up
monitoring with it in one pass for anyone who wants automatic discovery, but `process`
(`process_main_async()`, also a shell command) runs that exact same pass over cases already in the
database with zero Gmail dependency -- the intended default loop for a case added manually is
`add` -> `process` -> approve.

This is the "run one real case end-to-end" milestone. Everything real it wires together already
exists and is already tested in isolation:

- `plugins/discovery/gmail.GmailDiscoverySource` (issue #13) for discovery.
- `agent_loop.run_cycle()` (issue #10, made real by #34) for classify/draft.
- `plugins/llm/*` (issue #16) for a real `LLMProvider`.
- `plugins/legal_templates.RegistryLegalTemplateProvider` (issue #15/#34) for real draft content.
- `plugins/notifiers/console.ConsoleNotifier` (issue #9) for the human-approval gate (R1).
- `agent_loop.send_request()` + `plugins/senders/smtp.SMTPSender` (issue #35) for the real send.
- `plugins/jurisdiction_detection.detector.JurisdictionDetector` (issue #63; crawls the case's own
  site first, an optional SearXNG keyword search only as a fallback -- see
  `docs/user-guide/web-search.md`) to *propose* a jurisdiction for a genuinely new `Case`, which is
  now auto-applied directly (`_apply_jurisdiction_proposal()`, no prompt -- a deliberate reversal
  of issue #43's original manual-only design); a human can still review or override it any time
  before the case is classified via `case_actions.set_case_jurisdiction()`
  (`set-jurisdiction`/kanban).
- `rtbf_agent.followup.follow_up()` (issue #68) for the follow-up pass over every already-`Sent`
  case: `plugins/followup/monitor.InboxReplyMonitor` (over a read-only Gmail reply reader that
  reuses the same credentials as discovery) finds a reply, `plugins/followup/detector`.
  `ResponseClassifier` classifies its intent, and a terminal `Deletion confirmed`/`Rejected/
  Escalated` close happens only after explicit human confirmation via the same `Notifier` -- never
  auto-closed on the classification alone (R1's spirit, R9: a case with no reply is left untouched).

This module's only job is orchestration: resolve config, open the database, run discovery, create
`Case` rows for genuinely new `DiscoveredAccount`s (auto-applying a detected jurisdiction suggestion
for each with no prompt -- see `_apply_jurisdiction_proposal()`; a human can still override it, or
set drafting-language from scratch, any time before classification via `case_actions.
set_case_jurisdiction()`/`set_case_drafting_language()`), advance every not-yet-drafted `Case`
through `run_cycle()`, then -- for every
`Case` that is (now, or already) sitting in `Draft ready` -- prompt for the site's real
deletion-request recipient address (issue #52 -- manual only, never inferred; distinct from the
user's own scanned `target_email`) and solicit approval to send. It contains no
jurisdiction/language/site logic of its own (R20) and no code path here calls `send_request()` for
a `Case` outside `Draft ready` -- `send_request()` itself also guards that (raises `ValueError`
otherwise), so the check here is belt-and-suspenders, not the only line of defense (R1).

Config surfaces used here, all resolved from environment variables (no new config mechanism
invented beyond what issues #16/#35/#22 already established):

- `DATABASE_URL` -- reused from `.env.example`/`docs/specs` verbatim (a SQLAlchemy URL, e.g.
  `sqlite:///./data/rtbf-agent.db`). Not `RTBF_`-prefixed because it already existed in
  `.env.example` before this issue. Resolution now lives in `rtbf_agent.db.resolve_database_url()`
  (issue #53), shared verbatim with `api/` so the FastAPI app reads/writes the exact same database
  this module does -- not a separate one.
- `RTBF_TARGET_EMAIL` -- the mailbox address to scan and the email discovery attributes each
  resulting `Case` to. No prior name existed for this (this is the first module that needs it), so
  it follows this repo's established `RTBF_`-prefix convention.
- `RTBF_GMAIL_CLIENT_SECRETS_FILE` / `RTBF_GMAIL_TOKEN_FILE` -- Gmail OAuth (issue #13 defines the
  scope and the read-only client; it does not itself define how a CLI caller obtains and caches
  credentials, since that's orchestration, not discovery-plugin logic). `RTBF_GMAIL_TOKEN_FILE`
  defaults to `~/.config/rtbf-agent/gmail_token.json`, alongside this project's other local,
  gitignored config.
- `RTBF_LLM_PROVIDER`/`RTBF_LLM_*` (via `plugins.llm.config.resolve_llm_config`), `RTBF_SMTP_*`
  (via `plugins.senders.config.resolve_smtp_config`), `RTBF_REQUESTER_NAME`/
  `RTBF_DEFAULT_APPLICABLE_JURISDICTION`/`RTBF_DEFAULT_DRAFTING_LANGUAGE` (via
  `config.resolve_global_config`) -- all pre-existing, unchanged here.
- `RTBF_NOTIFIER_BACKEND` (issue #60) -- selects which `Notifier` `main_async()` wires up:
  `"console"` (the default -- unset behaves byte-for-byte identically to before this issue existed)
  or `"telegram"`, which builds a real `TelegramNotifier` (issue #14) from `TELEGRAM_BOT_TOKEN`/
  `TELEGRAM_CHAT_ID` (via `plugins.notifiers.config.resolve_telegram_config`) and
  `INTERFACE_LANGUAGE` (via `config.resolve_interface_language`, the same single-process-wide
  resolution `api/kanban.py`/`api/case_detail.py` already reuse -- not a second one invented here).
- `RTBF_REPLY_MONITOR_BACKEND` (issue #102) -- selects which `MailboxReader` follow-up monitoring
  (`_build_reply_monitor()`) uses: `"gmail"` (the default -- unset behaves byte-for-byte identically
  to before this issue existed) builds `GmailReplyReader` (issue #68) over the same read-only OAuth
  credentials discovery uses, or `"imap"`, which builds `ImapReplyReader` over password credentials
  (`RTBF_IMAP_*`, via `plugins.followup.imap_config.resolve_imap_config`) for a user with no Gmail
  OAuth app configured at all -- see that module and `plugins/followup/imap_reader.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shlex
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, select

from rtbf_agent import case_actions
from rtbf_agent.agent_loop import FOLLOWUP_MONITORED_STATES, run_cycle, send_request
from rtbf_agent.case_actions import CaseActionError
from rtbf_agent.config import GlobalConfig, resolve_global_config, resolve_interface_language
from rtbf_agent.core.interfaces import (
    DiscoveredAccount,
    DiscoverySource,
    LegalTemplateProvider,
    LLMProvider,
    NotificationMessage,
    Notifier,
    SearchResult,
    Sender,
    WebSearchProvider,
)
from rtbf_agent.core.models import CASE_STATES_IN_ORDER, Case, CaseState, Event, create_event
from rtbf_agent.db import build_engine, resolve_database_url
from rtbf_agent.followup import draft_additional_info_response, follow_up, selected_documents
from rtbf_agent.plugins.followup.detector import ResponseClassifier
from rtbf_agent.plugins.followup.monitor import InboxReplyMonitor
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider
from rtbf_agent.plugins.jurisdiction_detection.detector import (
    JurisdictionDetector,
    JurisdictionProposal,
)
from rtbf_agent.plugins.legal_templates.provider import RegistryLegalTemplateProvider
from rtbf_agent.plugins.llm.config import build_llm_provider, resolve_llm_config
from rtbf_agent.plugins.notifiers.config import resolve_telegram_config
from rtbf_agent.plugins.notifiers.console import ConsoleNotifier
from rtbf_agent.plugins.notifiers.telegram import PythonTelegramBotClient, TelegramNotifier
from rtbf_agent.plugins.recipient_detection.detector import (
    RecipientDetector,
    RecipientProposal,
)
from rtbf_agent.plugins.senders.smtp import SMTPSender
from rtbf_agent.plugins.webfetch.http import HTTPWebFetchProvider
from rtbf_agent.plugins.websearch.config import resolve_searxng_config
from rtbf_agent.plugins.websearch.searxng import SearXNGProvider
from rtbf_agent.setup_wizard import run_setup_wizard, stop_services

_logger = logging.getLogger(__name__)

_DEFAULT_NOTIFIER_BACKEND = "console"
_SUPPORTED_NOTIFIER_BACKENDS = (_DEFAULT_NOTIFIER_BACKEND, "telegram")

_DEFAULT_REPLY_MONITOR_BACKEND = "gmail"
_SUPPORTED_REPLY_MONITOR_BACKENDS = (_DEFAULT_REPLY_MONITOR_BACKEND, "imap")

DEFAULT_GMAIL_TOKEN_PATH = Path.home() / ".config" / "rtbf-agent" / "gmail_token.json"

# `run_cycle()` only ever advances a Case through Discovered -> Classified -> Draft ready and
# no-ops past that (see agent_loop.py). Selecting only cases at or before Draft ready here means
# a repeated invocation doesn't even query/loop over cases nothing here could ever advance --
# `run_cycle()`'s own internal no-op guarantees are the authoritative safety net (R9), this is just
# not doing redundant work on top of them.
_RUN_CYCLE_ELIGIBLE_STATES = (
    CaseState.DISCOVERED,
    CaseState.CLASSIFIED,
    CaseState.DRAFT_READY,
)


def _resolve_database_url() -> str:
    """Deprecated alias for `rtbf_agent.db.resolve_database_url()` -- kept for backwards compat.

    The logic moved to `rtbf_agent.db` (issue #53) so `api/` can share it without importing a
    private, leading-underscore name from this module. Nothing in this repo calls this alias
    anymore (see `main_async()` below), but it's kept in case anything outside this repo already
    imports `rtbf_agent.cli._resolve_database_url` directly.
    """
    return resolve_database_url()


def _resolve_target_email() -> str:
    target_email = os.environ.get("RTBF_TARGET_EMAIL")
    if not target_email:
        raise LookupError(
            "no target email configured: set RTBF_TARGET_EMAIL to the mailbox address to scan"
        )
    return target_email


def _load_cached_gmail_credentials() -> Any | None:
    """Load and, if needed/possible, refresh already-cached Gmail OAuth credentials -- never
    launches an interactive OAuth handshake. Returns `None` if there's no usable cached token
    (missing file, or an expired one whose refresh failed/was impossible), leaving the decision of
    whether to fall through to an interactive flow to the caller.

    Extracted from `_resolve_gmail_credentials()` (issue #86's automated-case-loop follow-up) so
    `_gmail_credentials_available_noninteractively()` can run the exact same cache-check without
    duplicating it -- an unattended `forget serve`/`process` pass needs to know whether follow-up
    monitoring is possible *without ever risking* `flow.run_local_server(port=0)`, which would hang
    forever with no TTY to complete it in.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from rtbf_agent.plugins.discovery.gmail import GMAIL_READONLY_SCOPE

    token_path_raw = os.environ.get("RTBF_GMAIL_TOKEN_FILE")
    token_path = Path(token_path_raw).expanduser() if token_path_raw else DEFAULT_GMAIL_TOKEN_PATH

    if not token_path.is_file():
        return None
    credentials = Credentials.from_authorized_user_file(
        str(token_path), scopes=[GMAIL_READONLY_SCOPE]
    )

    if credentials.valid:
        return credentials

    can_refresh = credentials.expired and credentials.refresh_token
    if not can_refresh:
        return None
    try:
        credentials.refresh(Request())
    except Exception:
        # A revoked/expired refresh token (google.auth.exceptions.RefreshError, or any other
        # transport failure) means this cached token is no longer usable non-interactively.
        return None

    token_path.parent.mkdir(parents=True, exist_ok=True)
    # `os.open(..., 0o600)` rather than `write_text()` + `.chmod()`: the latter leaves a brief
    # window where the token file exists under the process umask's (possibly world-readable)
    # default permissions before being restricted -- this OAuth refresh token is a credential and
    # should never be readable-by-default even momentarily.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(credentials.to_json())
    return credentials


def _gmail_credentials_available_noninteractively() -> bool:
    """`True` if Gmail credentials are already usable without launching an interactive OAuth
    handshake (issue #86's automated-case-loop follow-up).

    `process_main_async()`/`forget serve` run unattended, often with no TTY -- calling
    `_resolve_gmail_credentials()` there when no cached token exists (or a cached one has genuinely
    expired) would launch `flow.run_local_server(port=0)`, which blocks forever waiting for a
    browser redirect that will never come. This check answers "would that happen?" first, so
    follow-up monitoring can be skipped for this pass instead (R9: no hang, no crash, just no
    monitoring collaborator this time -- the human can still run `scan-inbox` interactively once to
    (re-)establish a cached token, after which unattended passes pick it up automatically).
    """
    return _load_cached_gmail_credentials() is not None


def _resolve_gmail_credentials() -> Any:
    """Resolve read-only Gmail OAuth credentials, completing/caching a handshake if needed.

    Extracted so every Gmail-backed capability -- inbox-scan discovery
    (`_build_gmail_discovery_source()`), the manual-URL registration cross-check, and follow-up
    reply monitoring (`_build_reply_monitor()`, issue #68) -- shares the *same* read-only
    credentials rather than each running its own OAuth flow. There is exactly one Gmail scope
    (`GMAIL_READONLY_SCOPE`) and follow-up introduces no new one: monitoring replies is another read
    against the same mailbox, never a write. Lazily imports the Google OAuth libraries for the same
    reason `plugins/discovery/gmail.py` does -- this is only reached from a real run, never a test.

    Tries `_load_cached_gmail_credentials()` first (no interactive flow); only falls through to a
    fresh, interactive `flow.run_local_server(port=0)` handshake if that returns `None` -- this is
    the one caller allowed to do that (a human running `scan-inbox`/`add` at a real terminal),
    unlike `_gmail_credentials_available_noninteractively()`'s callers.
    """
    from rtbf_agent.plugins.discovery.gmail import build_installed_app_flow

    client_secrets_file = os.environ.get("RTBF_GMAIL_CLIENT_SECRETS_FILE")
    if not client_secrets_file:
        raise LookupError(
            "no Gmail OAuth client configured: set RTBF_GMAIL_CLIENT_SECRETS_FILE to the path of "
            "an OAuth client secrets JSON file downloaded from Google Cloud Console"
        )

    credentials = _load_cached_gmail_credentials()
    if credentials is not None:
        return credentials

    token_path_raw = os.environ.get("RTBF_GMAIL_TOKEN_FILE")
    token_path = Path(token_path_raw).expanduser() if token_path_raw else DEFAULT_GMAIL_TOKEN_PATH

    flow = build_installed_app_flow(client_secrets_file)
    credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(credentials.to_json())

    return credentials


def _build_gmail_discovery_source(query: str | None = None) -> DiscoverySource:
    """Build a real `GmailDiscoverySource` over shared read-only Gmail credentials.

    `query` overrides `GmailDiscoverySource`'s default `category:primary` search query. The
    whole-inbox scan (`main_async()`) leaves it `None` (keeping that default); the manual-URL
    registration cross-check (`_build_gmail_registration_check_source()`) passes a domain-scoped
    query so the same subject-pattern-matching logic runs against just the pasted site's mail, not
    the whole inbox. Note: since `add-case` (the only production caller of
    `_build_gmail_registration_check_source()`) was removed in favor of the shell's `add` (which
    never ran this cross-check -- a pre-existing asymmetry), that builder currently has no
    production caller either; `add_case_for_url()`'s `registration_check_source` parameter it used
    to feed is still real, tested behavior, just presently reachable only from tests passing a fake
    source directly.
    """
    from rtbf_agent.plugins.discovery.gmail import GmailDiscoverySource, build_gmail_client

    client = build_gmail_client(_resolve_gmail_credentials())
    if query is not None:
        return GmailDiscoverySource(client, query=query)
    return GmailDiscoverySource(client)


def _build_gmail_registration_check_source(domain: str) -> DiscoverySource:
    """A `GmailDiscoverySource` scoped to just `domain`'s mail, for the manual-URL cross-check.

    The manual-URL path (issue #66) already knows the exact site the operator pasted, so instead
    of scanning the whole inbox it reuses the *same* `GmailDiscoverySource` subject-pattern logic
    (welcome/confirmation/verify/reset/created -- see `plugins/discovery/gmail._SUBJECT_PATTERNS`)
    against a Gmail search query narrowed to that one sender domain (`from:<domain>`). No new
    site-specific probing logic is written here: scoping is a pure Gmail-search-query concern, and
    everything downstream (subject matching, sender parsing, evidence shape) is
    `GmailDiscoverySource`'s unchanged behavior.

    Deliberately `from:<domain>` alone rather than `GmailDiscoverySource`'s default
    `category:primary from:<domain>`: the whole-inbox scan restricts to the primary category to
    bound volume across an entire mailbox, but a domain-scoped cross-check is already narrow, and a
    signup/confirmation mail from a manually-added site may just as well have landed in
    Promotions/Updates -- so the cross-check searches every category for that one sender to avoid a
    false "no evidence found" (R3: an honest absence must mean genuinely nothing, not "nothing in
    one category").
    """
    return _build_gmail_discovery_source(query=f"from:{domain}")


def _resolve_notifier_backend() -> str:
    """Resolve `RTBF_NOTIFIER_BACKEND` (issue #60), defaulting to `"console"` when unset/blank.

    An explicit value other than the two supported backends raises `ValueError` naming what was
    configured -- silently falling back to `"console"` on a typo (e.g. `RTBF_NOTIFIER_BACKEND=
    telgram`) would leave an operator who thinks they configured Telegram approval with a run that
    silently prompts on stdin instead, exactly the kind of "no error message explaining why" gap
    this issue exists to close.
    """
    raw = os.environ.get("RTBF_NOTIFIER_BACKEND", _DEFAULT_NOTIFIER_BACKEND).strip().lower()
    if not raw:
        return _DEFAULT_NOTIFIER_BACKEND
    if raw not in _SUPPORTED_NOTIFIER_BACKENDS:
        raise ValueError(
            f"unsupported RTBF_NOTIFIER_BACKEND {raw!r}: expected one of "
            f"{', '.join(sorted(_SUPPORTED_NOTIFIER_BACKENDS))!r}"
        )
    return raw


def _resolve_reply_monitor_backend() -> str:
    """Resolve `RTBF_REPLY_MONITOR_BACKEND` (issue #102), defaulting to `"gmail"` when unset/blank.

    Mirrors `_resolve_notifier_backend()`'s exact shape (issue #60): an explicit value other than
    the two supported backends raises `ValueError` naming what was configured, rather than silently
    falling back to `"gmail"` on a typo (e.g. `RTBF_REPLY_MONITOR_BACKEND=imp`) -- an operator who
    thinks they configured IMAP-based follow-up monitoring must not silently get Gmail's OAuth
    requirement instead with no indication why, the same gap issue #60 closed for the notifier
    backend.
    """
    raw = (
        os.environ.get("RTBF_REPLY_MONITOR_BACKEND", _DEFAULT_REPLY_MONITOR_BACKEND).strip().lower()
    )
    if not raw:
        return _DEFAULT_REPLY_MONITOR_BACKEND
    if raw not in _SUPPORTED_REPLY_MONITOR_BACKENDS:
        raise ValueError(
            f"unsupported RTBF_REPLY_MONITOR_BACKEND {raw!r}: expected one of "
            f"{', '.join(sorted(_SUPPORTED_REPLY_MONITOR_BACKENDS))!r}"
        )
    return raw


def _build_telegram_notifier() -> TelegramNotifier:
    """Build a real, notify-only `TelegramNotifier` (issue #60).

    `resolve_telegram_config()` raises `LookupError` (naming exactly which env var is missing) if
    `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't both set -- propagated as-is rather than caught
    here, since there's no sensible way to notify over Telegram without them. No `Application`/
    polling lifecycle needed anymore -- `notify()` is a single outbound API call, nothing waits on
    an incoming update (see `TelegramNotifier`'s module docstring).
    """
    telegram_config = resolve_telegram_config()
    bot = PythonTelegramBotClient(telegram_config.bot_token)
    lang = resolve_interface_language()
    return TelegramNotifier(
        bot=bot, chat_id=telegram_config.chat_id, i18n=ResourceI18nProvider(), lang=lang
    )


def _build_llm_provider() -> LLMProvider:
    """Thin `cli.py`-local alias for `plugins.llm.config.build_llm_provider()` (moved there so
    `api/document.py`'s draft route can share the exact same protocol-selection logic -- see that
    function's own docstring). Kept here, rather than replacing every call site with the fully
    qualified import, purely to avoid a mechanical rename across this already-large module."""
    return build_llm_provider()


def _dedup_key(domain: str, target_email: str) -> tuple[str, str]:
    return (domain, target_email)


def _apply_jurisdiction_proposal(
    case: Case, jurisdiction_proposal: JurisdictionProposal | None
) -> None:
    """Auto-apply a detected jurisdiction proposal onto a genuinely new `Case`, non-interactively.

    Replaces the old `prompt_case_overrides()` (issue #43's manual-only design, deliberately
    reversed by a later maintainer decision -- see this function's callers' docstrings): no prompt
    anywhere a case is created, in `run_once()`'s per-created-case loop or `add_case_for_url()`.
    A genuine proposal (non-`None`) is applied directly onto `Case.applicable_jurisdiction`; no
    proposal (detection disabled, or `_propose_jurisdiction()` returning `None` for a failure or a
    genuine "couldn't determine" outcome) leaves the field `None`, silently deferring to the global
    default later exactly as `Case.resolved_applicable_jurisdiction` already does for any case with
    no per-case override. `_propose_jurisdiction()` itself already records the full audit trail
    (`jurisdiction_proposed`/`jurisdiction_no_proposal`/`jurisdiction_detection_failed`) regardless
    of what its caller does with the return value, so *why* a suggestion was or wasn't applied is
    always on the case's own history either way.

    `drafting_language` has no detector at all (nothing proposes it), so there is nothing to
    auto-apply for it here -- it simply stays `None` until a human sets it explicitly
    (`case_actions.set_case_drafting_language()`) or the global default takes over at
    classification time. This is the *only* place a `JurisdictionProposal` can ever reach
    `Case.applicable_jurisdiction`; nothing in `plugins/jurisdiction_detection/` writes to a `Case`
    directly (R1's spirit, extended to jurisdiction selection). A human can still review or
    override the applied value (or set either field from scratch) any time before classification,
    via `case_actions.set_case_jurisdiction()`/`set_case_drafting_language()` -- the
    `set-jurisdiction`/`set-drafting-language` shell/kanban commands (R22: independent axes, no
    package deal).
    """
    if jurisdiction_proposal is not None:
        case.applicable_jurisdiction = jurisdiction_proposal.jurisdiction


def create_cases_for_new_accounts(
    session: Session, accounts: Sequence[DiscoveredAccount]
) -> list[Case]:
    """Create one `Case` per `DiscoveredAccount` not already represented by an existing `Case`.

    Dedup key: `(domain, target_email)` -- a `DiscoveredAccount.domain` is the same site the
    resulting `Case.domain` records, and `target_email` is the same mailbox the resulting
    `Case.target_email` records; a `Case` with matching values for both already represents this
    account regardless of which discovery run originally created it (site display name/evidence
    may legitimately differ across runs for the same underlying account, so neither is part of the
    key). Returns only the newly-created `Case` rows (already added to `session`, not yet committed
    by this function -- the caller commits once for the whole batch).
    """
    existing = session.exec(select(Case.domain, Case.target_email)).all()
    existing_keys = {_dedup_key(domain, target_email) for domain, target_email in existing}

    created: list[Case] = []
    for account in accounts:
        key = _dedup_key(account.domain, account.target_email)
        if key in existing_keys:
            continue
        case = Case(
            site=account.site,
            domain=account.domain,
            target_email=account.target_email,
            discovery_evidence=json.dumps(dict(account.evidence)),
        )
        session.add(case)
        created.append(case)
        existing_keys.add(key)  # guards against duplicate accounts within the same discover() call
    return created


# Evidence-mapping key under which a manual-URL discovery records the URL the operator pasted, so a
# `Case` created this way is distinguishable in its `discovery_evidence` from one a Gmail scan
# created (which records `email_id`/`matched_pattern`/`matched_header` instead -- see
# `plugins/discovery/gmail.py`). Both populate the same `Case.discovery_evidence` JSON field.
MANUAL_URL_EVIDENCE_SOURCE = "manual_url"

# `discovery_evidence` key under which the registration cross-check records its outcome (issue #66).
# Present on a manually-added Case whether the cross-check found matching inbox mail or not -- an
# honest "nothing found" is recorded explicitly, never silently dropped (R3).
REGISTRATION_CHECK_KEY = "registration_check"
_REGISTRATION_CHECK_ABSENT = "no matching email found in inbox"
# Distinct from `_REGISTRATION_CHECK_ABSENT`: "the check ran and genuinely found nothing" and "the
# check could not run" are different facts, and conflating them would let a transient Gmail error
# masquerade as a confident "nothing found" (undermining R3's honest-absence guarantee).
_REGISTRATION_CHECK_FAILED = "registration check could not be completed (discovery error)"


def extract_domain(url: str) -> str:
    """Extract the bare host/domain from a pasted URL or bare domain (issue #66).

    Handles a full URL (`https://User@Example.com:8443/path?q=1#frag` -> `example.com`) and a bare
    domain (`Example.com/signup` -> `example.com`) uniformly by leaning on `urllib.parse.urlsplit`
    (never a hand-rolled regex): a string with no `//` is prefixed with `//` so `urlsplit` parses
    it as a network location rather than a bare path, then `.hostname` strips any userinfo/port and
    lowercases the host for us. A leading `www.` is dropped so a pasted `https://www.example.com`
    dedupes against a Gmail-discovered `example.com` (whose domain comes from a `From:` address like
    `noreply@example.com`, which never carries a `www.` label) -- `(domain, target_email)` is the
    dedup key both discovery paths share (see `create_cases_for_new_accounts`).

    Raises `ValueError` for input no host can be extracted from (empty, whitespace, or e.g. a lone
    scheme) rather than silently producing an empty-string domain that would create a junk `Case`.
    """
    raw = url.strip()
    if not raw:
        raise ValueError("cannot extract a domain from an empty URL")
    # `urlsplit` only populates `.hostname` when the input has a network-location component; a bare
    # domain/path like "example.com/signup" has none, so prefix "//" to force netloc parsing.
    if "//" not in raw:
        raw = "//" + raw
    host = urlsplit(raw).hostname
    if not host:
        raise ValueError(f"could not extract a domain from {url!r}")
    if host.startswith("www."):
        host = host[len("www.") :]
    if not host:
        raise ValueError(f"could not extract a domain from {url!r}")
    return host


async def add_case_for_url(
    session: Session,
    url: str,
    target_email: str,
    *,
    registration_check_source: DiscoverySource | None = None,
    jurisdiction_detector: JurisdictionDetector | None = None,
    print_fn: Callable[[str], None] = print,
) -> Case:
    """Create (or dedupe to an existing) `Case` for a site the operator already knows about (#66).

    The second, human-initiated discovery path alongside the Gmail inbox scan: rather than waiting
    for `GmailDiscoverySource` to surface a site, the operator pastes its URL and a `Case` is
    created for it directly. Crucially this does *not* fork a second `Case`-creation code path --
    it constructs a synthetic `DiscoveredAccount` for the pasted domain and runs it through the
    exact same `create_cases_for_new_accounts()` the Gmail path uses, so the `(domain,
    target_email)` dedup key and `Case` construction are shared verbatim. A manually-added site
    that later also shows up via a Gmail scan (or vice versa) therefore never produces a duplicate
    `Case`. Returns the `Case` -- the freshly-created one, or the pre-existing one a dedup hit
    resolved to.

    `registration_check_source` (optional) is a domain-scoped `DiscoverySource` -- in a real run a
    `GmailDiscoverySource` narrowed to `from:<domain>` (see
    `_build_gmail_registration_check_source()`); in tests a fake -- used to cross-check whether the
    inbox already carries registration evidence for this domain. That cross-check is purely
    informational and runs *after* the `Case` is already committed, so it never blocks, delays, or
    can undo case creation (issue #66; the same "independent signals against one Case" framing as
    jurisdiction detection #63 and recipient detection #64). Its outcome -- evidence found *or* an
    explicit "nothing found" -- is always recorded onto `Case.discovery_evidence`, never merely
    printed (R3). Passing `None` skips the cross-check entirely.

    `jurisdiction_detector` (issue #63 parity follow-up, no-prompt reversal) closes a gap this
    function used to have relative to `run_once()`'s per-created-case loop: a case created here
    used to get no jurisdiction detection at all, silently falling back to the global default the
    first time it was classified -- unlike a Gmail-discovered case, which gets it at creation time.
    A genuine proposal is now auto-applied right here, at creation time (`_apply_jurisdiction_
    proposal()`, no prompt -- see that function's docstring), exactly like a Gmail-discovered case.
    This only happens for a genuinely new (`created`) case, mirroring the "only genuinely new Cases"
    rule `run_once()`'s loop already documents -- a dedup hit onto a pre-existing `Case` never gets
    a fresh proposal applied here even if a detector is given, since this is meant to run once,
    right after creation. Defaults to `None`, which reproduces this function's original behavior
    byte-for-byte (no detection, no auto-apply) -- an existing caller that never passes it sees no
    change. `drafting_language` is never proposed by anything and stays `None` here, deferring to
    the global default exactly as `Case.resolved_drafting_language` already does -- see
    `case_actions.set_case_drafting_language()` for the explicit override path.
    """
    domain = extract_domain(url)
    account = DiscoveredAccount(
        site=domain,  # a pasted URL carries no display name; the domain is the best label we have
        domain=domain,
        target_email=target_email,
        evidence={"source": MANUAL_URL_EVIDENCE_SOURCE, "url": url},
    )
    created = create_cases_for_new_accounts(session, [account])
    if created:
        case = created[0]
        print_fn(f"Created case for {domain} (target email {target_email}).")
        if jurisdiction_detector is not None:
            # Assign an id without committing yet -- _propose_jurisdiction()'s jurisdiction_proposed
            # Event needs a real case_id -- mirrors run_once()'s identical flush()-before-detection
            # step.
            session.flush()
        proposal = None
        if jurisdiction_detector is not None:
            proposal = await _propose_jurisdiction(
                session, case, jurisdiction_detector, print_fn=print_fn
            )
        _apply_jurisdiction_proposal(case, proposal)
    else:
        case = session.exec(
            select(Case).where(Case.domain == domain, Case.target_email == target_email)
        ).one()
        print_fn(
            f"A case for {domain} (target email {target_email}) already exists "
            f"(id {case.id}) -- not creating a duplicate."
        )
    session.commit()  # persist before the (non-blocking, informational) cross-check runs

    if registration_check_source is not None:
        await _cross_check_registration(
            case, domain, target_email, registration_check_source, print_fn=print_fn
        )
        session.commit()
    return case


async def _cross_check_registration(
    case: Case,
    domain: str,
    target_email: str,
    registration_check_source: DiscoverySource,
    *,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Record whether the inbox holds registration evidence for `domain`, onto `discovery_evidence`.

    Runs the domain-scoped `registration_check_source.discover()` (reusing `GmailDiscoverySource`'s
    unchanged subject-pattern matching, just narrowed to this one sender domain) and merges the
    outcome into `case.discovery_evidence` under `REGISTRATION_CHECK_KEY`, alongside the manual-URL
    evidence already there. Both outcomes are recorded (R3): a match stores the discovered
    account(s)' evidence; a miss stores an explicit `"no matching email found in inbox"` sentinel,
    never a silently-absent key.

    The source is scoped to `domain`, but its results are still filtered here so a hypothetical
    broader query (or a future differently-scoped source) can never attribute another site's mail
    to this `Case`. The filter accepts `domain` itself *and* any subdomain of it (`*.domain`): real
    signup/confirmation mail very commonly comes from a sending subdomain (e.g. `noreply@
    e.example.com` for `example.com`), and Gmail's own `from:example.com` query already returns
    those -- dropping them would manufacture exactly the false "nothing found" (R3) this cross-check
    exists to avoid. The `.`-anchored suffix check (`.domain`, not a bare `endswith(domain)`) is
    deliberate: it matches `e.example.com` but never a look-alike like `notexample.com` or
    `example.com.evil.com`, so Gmail's loose substring `from:` matching can't smuggle in a foreign
    sender.

    Best-effort, exactly like its siblings `_propose_jurisdiction()`/`_propose_recipient()`: a
    failure of the underlying discovery (Gmail transport error, expired auth, quota) must never
    crash the operation -- the `Case` is already committed by the time this runs, and this step is
    explicitly informational. Any exception is caught and recorded as a *distinct* "could not be
    completed" outcome (never the "nothing found" sentinel, which would falsely imply a confident
    negative -- R3). The broad `except Exception` mirrors the deliberate degradation boundary those
    two siblings document.
    """
    evidence: dict[str, Any] = (
        json.loads(case.discovery_evidence) if case.discovery_evidence else {}
    )
    try:
        accounts = await registration_check_source.discover(target_email)
    except Exception:
        evidence[REGISTRATION_CHECK_KEY] = _REGISTRATION_CHECK_FAILED
        case.discovery_evidence = json.dumps(evidence)
        print_fn(
            f"Registration cross-check could not be completed for {domain} (discovery error) "
            "-- recorded explicitly; case creation is unaffected."
        )
        return

    matches = [
        account
        for account in accounts
        if account.domain == domain or account.domain.endswith(f".{domain}")
    ]
    if matches:
        evidence[REGISTRATION_CHECK_KEY] = {
            "status": "evidence_found",
            "matches": [dict(account.evidence) for account in matches],
        }
        print_fn(
            f"Registration cross-check: found {len(matches)} matching email(s) from {domain} "
            "in the inbox."
        )
    else:
        evidence[REGISTRATION_CHECK_KEY] = _REGISTRATION_CHECK_ABSENT
        print_fn(
            f"Registration cross-check: {_REGISTRATION_CHECK_ABSENT} for {domain} "
            "(recorded explicitly)."
        )
    case.discovery_evidence = json.dumps(evidence)


_LOGGED_SNIPPET_MAX_CHARS = 500
"""Cap on a search result's `snippet` when it's written to `Event.detail` -- matches the
`_MAX_SNIPPET_CHARS` each detector module already applies before a snippet reaches the LLM prompt
(`plugins/jurisdiction_detection/detector.py`, `plugins/recipient_detection/detector.py`), which
`JurisdictionAttempt.results`/`RecipientAttempt.results` do *not* apply -- they carry the raw
`SearchResult`s `propose()` got back from `WebSearchProvider.search()`. Without this cap here, a
result whose upstream `content` field is unusually large (or adversarially large, on a hostile
site's own indexed page) would reach a JSON DB column with no size limit and then render
untruncated on the case-detail page -- capping independently at the point of logging, rather than
trusting the results were already bounded by prompt-building code elsewhere, keeps this correct
even if that unrelated cap ever changes."""


def _search_results_detail(results: tuple[SearchResult, ...]) -> list[dict[str, str]]:
    """Plain-dict form of `SearchResult`s for an `Event.detail` (JSON column, no dataclasses).

    Shared by `_propose_jurisdiction()`/`_propose_recipient()` so both the successful-proposal and
    no-proposal event kinds record the same evidence shape -- the whole point of this PR is that a
    "couldn't determine" outcome is just as auditable as a confident one. `snippet` is truncated to
    `_LOGGED_SNIPPET_MAX_CHARS` -- see that constant's docstring for why this must be its own cap,
    not an assumption that `results` was already bounded upstream.
    """
    return [
        {"title": r.title, "url": r.url, "snippet": r.snippet[:_LOGGED_SNIPPET_MAX_CHARS]}
        for r in results
    ]


async def _propose_jurisdiction(
    session: Session,
    case: Case,
    jurisdiction_detector: JurisdictionDetector,
    *,
    print_fn: Callable[[str], None] = print,
    verbose: bool = False,
) -> JurisdictionProposal | None:
    """Best-effort jurisdiction research for `case` (issue #63) -- never raises.

    Returns `None` on *any* failure (web search unreachable/misconfigured, LLM transport error,
    an unexpected exception from either collaborator) as well as on the detector's own non-error
    "couldn't determine" outcome (`JurisdictionDetector.propose()` already returns `None` for that
    case) -- both are, by design, indistinguishable to this function's caller: either way, the
    manual prompt falls through to issue #43's original behavior (global-default-as-blank-answer),
    exactly as if detection were disabled. This is the one deliberate broad-except boundary this
    module has: catching anything from an *optional* enhancement so it can never crash a run that
    would otherwise have succeeded without it, per issue #63's explicit "must degrade gracefully"
    requirement -- a narrower except clause here would risk a new, unanticipated failure mode of
    the (still-young) `plugins/websearch/`/`plugins/llm/` stack taking down the whole CLI run.

    On a genuine proposal, records a `jurisdiction_proposed` `Event` (case must already be
    persisted -- have an `id` -- for that) with the query/results/reasoning/sources so a human can
    later see *why* the agent suggested what it did, same spirit as `agent_loop._classify()`'s
    `classification_proposed` `Event`. Recording that `Event` here, rather than only after the
    human responds to the prompt, means the proposal is on the audit trail even if a human ends up
    overriding it -- what the agent suggested is itself worth keeping, not just what was ultimately
    accepted.

    On a failed collaborator call or a "couldn't determine" outcome, records
    `jurisdiction_detection_failed`/`jurisdiction_no_proposal` respectively -- real report: a case's
    audit trail previously went silent in both situations (only a console `print_fn` line, gone once
    the shell session ends), so a human reviewing the case later via the kanban UI had no way to
    tell "detection was never attempted" apart from "detection tried and came up empty" apart from
    "detection is silently broken". `jurisdiction_no_proposal` includes the query/results actually
    searched, same as a successful proposal, so the evidence is on the record either way.

    `print_fn` reports *why* no suggestion appeared (a real error, vs. a genuine "search/LLM
    couldn't determine one") -- real report: an operator whose search/LLM stack was silently
    failing had no way to tell "detection is degrading gracefully" apart from "detection was never
    attempted", since both looked identical (a bare manual prompt, no signal either way). This
    keeps the "never raise" contract exactly as-is; it only adds visibility into a `None` this
    function was always going to return anyway.

    `verbose` (issue #98) additionally prints a line via `print_fn` when a genuine proposal *is*
    recorded -- the failure/no-proposal lines above already print unconditionally (pre-existing
    behavior, unchanged); this is strictly additive and defaults to `False` so a caller that never
    passes it sees no new output.
    """
    if case.id is None:
        raise RuntimeError(
            "case must be persisted (have an id) before requesting a jurisdiction proposal"
        )
    try:
        attempt = await jurisdiction_detector.propose(site=case.site, domain=case.domain)
    except Exception as exc:
        print_fn(f"Jurisdiction research for {case.domain} failed ({exc}) -- asking manually.")
        session.add(
            create_event(
                case_id=case.id,
                kind="jurisdiction_detection_failed",
                actor="agent",
                detail={"error": str(exc)},
            )
        )
        return None
    if attempt.proposal is None:
        print_fn(
            f"No confident jurisdiction suggestion found for {case.domain} -- asking manually."
        )
        session.add(
            create_event(
                case_id=case.id,
                kind="jurisdiction_no_proposal",
                actor="agent",
                detail={
                    "query": attempt.query,
                    "results": _search_results_detail(attempt.results),
                },
            )
        )
        return None

    proposal = attempt.proposal
    session.add(
        create_event(
            case_id=case.id,
            kind="jurisdiction_proposed",
            actor="agent",
            detail={
                "jurisdiction": proposal.jurisdiction,
                "reasoning": proposal.reasoning,
                "sources": list(proposal.sources),
                "query": attempt.query,
                "results": _search_results_detail(attempt.results),
            },
        )
    )
    if verbose:
        print_fn(
            f"[case {case.site} ({case.domain})] jurisdiction proposal recorded: "
            f"{proposal.jurisdiction!r}"
        )
    return proposal


async def _propose_recipient(
    session: Session,
    case: Case,
    recipient_detector: RecipientDetector,
    *,
    print_fn: Callable[[str], None] = print,
    verbose: bool = False,
) -> RecipientProposal | None:
    """Best-effort recipient-contact research for `case` (issue #64) -- never raises on a
    collaborator failure (the `case.id is None` check below is a programmer-error assertion, not a
    runtime-degradation path -- every real caller passes an already-persisted `case`).

    The exact sibling of `_propose_jurisdiction()`: returns `None` on *any* failure (web search
    unreachable/misconfigured, LLM transport error, an unexpected exception from either
    collaborator) as well as on the detector's own non-error "couldn't determine" outcome
    (`RecipientDetector.propose()` already returns `None` for that case) -- both are, by design,
    indistinguishable to this function's caller: either way, the case is simply left with no
    recipient suggestion, and `case_actions.confirm_recipient()` (kanban or CLI) falls through to
    issue #52's required-manual-entry behavior with no suggestion, exactly as if
    detection were disabled. This is a deliberate broad-except boundary, catching anything from an
    *optional* enhancement so it can never crash a run that would otherwise have succeeded -- issue
    #64's explicit "must degrade gracefully" requirement, and doubly important here because a
    recipient guess feeds a real send: a failure to research one must never do more than fall back
    to asking the human, never abort the whole run.

    On a genuine proposal, records a `recipient_proposed` `Event` (case must already be persisted --
    have an `id`) with the query/results/reasoning/sources so a human can later audit *why* the
    agent suggested what it did, same spirit as `jurisdiction_proposed`. Recording it here, before
    the human responds to the prompt, keeps what the agent suggested on the audit trail even when
    the human overrides it -- crucial for a value a wrong guess could misdirect a real request to.

    On a failed collaborator call or a "couldn't determine" outcome, records
    `recipient_detection_failed`/`recipient_no_proposal` respectively -- see
    `_propose_jurisdiction()`'s identical addition for the real report that motivated this (a
    case's audit trail previously went silent in both situations).

    `print_fn` reports *why* no suggestion appeared -- see `_propose_jurisdiction()`'s identical
    addition for the real report that motivated this (an operator with a silently-failing search/
    LLM stack had no way to tell a real failure apart from a genuine "couldn't determine", since
    both looked identical: a bare manual prompt with no explanation).

    `verbose` (issue #98) is the exact sibling of `_propose_jurisdiction()`'s: prints a line when a
    genuine proposal is recorded, additive and default-`False`.
    """
    if case.id is None:
        raise RuntimeError(
            "case must be persisted (have an id) before requesting a recipient proposal"
        )
    try:
        attempt = await recipient_detector.propose(site=case.site, domain=case.domain)
    except Exception as exc:
        print_fn(f"Recipient research for {case.domain} failed ({exc}) -- asking manually.")
        session.add(
            create_event(
                case_id=case.id,
                kind="recipient_detection_failed",
                actor="agent",
                detail={"error": str(exc)},
            )
        )
        return None
    if attempt.proposal is None:
        print_fn(f"No confident recipient suggestion found for {case.domain} -- asking manually.")
        session.add(
            create_event(
                case_id=case.id,
                kind="recipient_no_proposal",
                actor="agent",
                detail={
                    "query": attempt.query,
                    "results": _search_results_detail(attempt.results),
                },
            )
        )
        return None

    proposal = attempt.proposal
    session.add(
        create_event(
            case_id=case.id,
            kind="recipient_proposed",
            actor="agent",
            detail={
                "recipient": proposal.recipient,
                "reasoning": proposal.reasoning,
                "sources": list(proposal.sources),
                "query": attempt.query,
                "results": _search_results_detail(attempt.results),
            },
        )
    )
    if verbose:
        print_fn(
            f"[case {case.site} ({case.domain})] recipient proposal recorded: "
            f"{proposal.recipient!r}"
        )
    return proposal


async def run_once(
    session: Session,
    discovery_source: DiscoverySource | None,
    target_email: str,
    llm: LLMProvider,
    notifier: Notifier,
    legal_provider: LegalTemplateProvider,
    global_config: GlobalConfig,
    sender: Sender,
    *,
    model: str,
    print_fn: Callable[[str], None] = print,
    jurisdiction_detector: JurisdictionDetector | None = None,
    recipient_detector: RecipientDetector | None = None,
    reply_monitor: InboxReplyMonitor | None = None,
    response_classifier: ResponseClassifier | None = None,
    verbose: bool = False,
) -> None:
    """One full discover -> case -> loop -> approval -> send -> follow-up -> resend pass.
    Repeatable (R9).

    `model` (required, no default -- see `agent_loop._classify()`'s docstring for why) must be the
    real, resolved model name for `llm` (e.g. `resolve_llm_config().model`); forwarded verbatim to
    `run_cycle()`'s classify step.

    `discovery_source` is optional (issue #86) -- `None` skips the whole discovery/case-creation
    block below entirely (never calls `.discover()`), while everything after it (the `run_cycle()`
    batch over every `Case` already at or before `Draft ready`, the recipient-confirm + send loop,
    and the optional follow-up pass) runs exactly as it would otherwise. This is what lets a
    manually-added `Case` (the interactive shell's `add`, issue #66/#82) be classified,
    drafted, and sent without ever requiring Gmail OAuth -- Gmail discovery is one optional way
    *into* this pipeline, never a precondition for the rest of it to run (`cli.process_main_async()`
    is the real caller that omits it; `cli.main_async()`/`scan-inbox` still passes a real one,
    unchanged).

    Every collaborator (`llm`, `notifier`, `legal_provider`, `sender`) is passed in rather than
    constructed here, so this function can be exercised in tests against fakes/stubs without a
    real Gmail account, LLM vendor, SMTP server, or interactive terminal (`main_async()` is the
    only caller that wires the real ones).

    `jurisdiction_detector` (issue #63) is optional and defaults to `None` -- omitting it leaves
    every genuinely new `Case`'s `applicable_jurisdiction` untouched (`None`, deferring to the
    global default later). When given, each genuinely new `Case` is (best-effort, see
    `_propose_jurisdiction()`) researched, and any resulting proposal is auto-applied directly onto
    `Case.applicable_jurisdiction` -- no prompt (`_apply_jurisdiction_proposal()`; a deliberate
    maintainer-directed reversal of issue #43's original manual-only design). A human can still
    review or override the applied value (or set it from scratch) any time before the case is
    classified, via `case_actions.set_case_jurisdiction()` (the `set-jurisdiction` shell/kanban
    command) -- `drafting_language` has no detector at all and is likewise editable via
    `set_case_drafting_language()`/`set-drafting-language`.

    `recipient_detector` (issue #64) is likewise optional and defaults to `None`. Unlike
    jurisdiction detection (which runs once at case-creation time), it runs right before send, for
    each `Case` in `Draft ready` that still lacks a recipient, researching a *suggestion* (recorded
    as a `recipient_proposed` `Event`) a human confirms or corrects via `case_actions.
    confirm_recipient()` (kanban or CLI) whenever they get to it -- never written to
    `Case.recipient_email` on its own, and never blocked on here (R9): a case with no recipient
    confirmed yet is simply skipped this pass.

    Never advances or sends for a `Case` outside the state it's eligible for: cases are only
    fetched into the `run_cycle()` batch if they're at or before `Draft ready`, and
    `send_request()` is only ever called for cases actually observed in `Draft ready` (with a
    confirmed recipient) by a query issued *after* that batch runs -- so a case already
    `Awaiting response`/`Rejected/Escalated`/anything else from a prior invocation is never
    touched here.

    `reply_monitor`/`response_classifier` (issue #68) are optional and both default to `None`;
    supplying *both* enables a final follow-up pass over every `Case` in `Awaiting response`
    (`follow_up()` -- monitor for a reply, classify it, and act: a `CONFIRMATION` closes the case
    immediately as `Deletion confirmed`, no separate human click, per a later maintainer-directed
    narrowing of issue #68 -- see `followup.py`'s module docstring; a `REJECTION` still only
    notifies, resolved separately via `case_actions.confirm_followup()`/`dispute_followup()`).
    Omitting either (as every caller did before this issue existed) skips
    follow-up entirely, leaving those cases exactly as they were -- and even when enabled, a case
    with no new reply is left byte-for-byte untouched (R9). They are threaded through with `None`
    defaults precisely so existing callers/tests are unaffected.

    Unlike an earlier version of this pipeline (issue #69/#117, superseded -- see `followup.py`'s
    module docstring, "Why drafting is now an explicit, synchronous action"), `run_cycle()` no
    longer includes an automatic "draft the next additional-info round" pass: drafting a reply to
    a site's additional-info request is now a direct, explicit human action
    (`followup.draft_additional_info_response()`, kanban's "Draft" button or the CLI's
    `draft-additional-info` subcommand), not something a background poll guesses has become ready
    -- there is nothing left here for a recurring pass to do on a human's behalf for that step.

    `verbose` (issue #98) is strictly additive/opt-in -- default `False` reproduces today's exact
    output (silence beyond the pre-existing `print_fn` calls above, which fire unconditionally
    regardless of this flag). When `True`, this prints one line via `print_fn` for each of: a case
    advancing through `run_cycle()` (classified/drafted, detected by a `Case.state` change), a
    jurisdiction/recipient proposal actually recorded (threaded into `_propose_jurisdiction()`/
    `_propose_recipient()`), a case actually sent (`send_request()` moving it out of `Draft ready`),
    whether the follow-up pass ran at all this invocation and why not when it didn't (without
    changing the `reply_monitor is not None and response_classifier is not None` check itself --
    issue #99 owns that logic), and a reply found and classified during the follow-up pass (detected
    by a new `response_classified` `Event` for that case). None of this changes what the pipeline
    actually does -- only what gets printed.
    """
    if discovery_source is not None:
        accounts = await discovery_source.discover(target_email)
        created = create_cases_for_new_accounts(session, accounts)
        if created and jurisdiction_detector is not None:
            # Assign ids without committing yet -- _propose_jurisdiction()'s jurisdiction_proposed
            # Event needs a real case_id, but nothing here should become visible to another
            # session until this whole batch's per-case detection/auto-apply is done (mirrors the
            # single commit() below, just deferred one step earlier only when detection is in play).
            session.flush()
        # Only genuinely new Cases (issue #43) -- `created` is exactly the not-already-represented
        # accounts this batch produced, so a pre-existing Case (already past DISCOVERED, or simply
        # created by an earlier invocation) is never re-prompted here, regardless of whatever
        # per-case jurisdiction/drafting-language values it may or may not already carry.
        for case in created:
            proposal = None
            if jurisdiction_detector is not None:
                proposal = await _propose_jurisdiction(
                    session, case, jurisdiction_detector, print_fn=print_fn, verbose=verbose
                )
            _apply_jurisdiction_proposal(case, proposal)
        session.commit()

    # Filtered in Python rather than `.where(Case.state.in_(...))`: `ty` (this project's type
    # checker) doesn't resolve `.in_()` on the `CaseState` enum-typed column descriptor the same
    # way SQLAlchemy's runtime does (mirrors `agent_loop.latest_draft()`'s choice to sort `Event`s
    # in Python rather than via `.order_by()` for an analogous typing mismatch).
    eligible_cases = [
        case
        for case in session.exec(select(Case)).all()
        if case.state in _RUN_CYCLE_ELIGIBLE_STATES
    ]
    for case in eligible_cases:
        prior_state = case.state
        await run_cycle(session, case, llm, notifier, legal_provider, global_config, model=model)
        session.commit()
        if verbose and case.state != prior_state:
            print_fn(
                f"[case {case.site} ({case.domain})] advanced: "
                f"{prior_state.value!r} -> {case.state.value!r}"
            )

    draft_ready_cases = session.exec(select(Case).where(Case.state == CaseState.DRAFT_READY)).all()
    for case in draft_ready_cases:
        # Research a *suggested* recipient contact (issue #64, best-effort -- `_propose_recipient()`
        # never raises) for a case that still lacks one -- recorded as a `recipient_proposed` Event
        # a human sees and confirms (or corrects) via kanban/shell (`case_actions.
        # confirm_recipient()`), never written to `Case.recipient_email` on its own.
        if recipient_detector is not None and not case.recipient_email:
            await _propose_recipient(
                session, case, recipient_detector, print_fn=print_fn, verbose=verbose
            )
            session.commit()

        # A case with no recipient confirmed yet is simply left untouched this pass -- no blocking
        # wait (R9). `send_request()` itself also refuses to send without one; this check just
        # avoids the `ValueError` it would otherwise raise for that already-expected case.
        if not case.recipient_email:
            continue
        prior_state = case.state
        # `selected_documents()`, not a bare `send_request(session, case, sender)` (real report): a
        # `Draft ready` case reached via an additional-info round's document-attach path (issue
        # #69/#111) still has its vault documents selected but unused at this point -- neither
        # `agent_loop.compose_additional_info_draft()` nor `case_actions.approve_draft()` ever calls
        # `send_request()` themselves (see their own docstrings), so it's *this* loop that actually
        # dispatches the send once a human approves. Passing no documents here silently sent every
        # such resend with no attachment at all -- `()` for any ordinary first-time-send case
        # (nothing selected), so this is a no-op change for every other case.
        documents = selected_documents(session, case.id) if case.id is not None else []
        await send_request(session, case, sender, documents)
        session.commit()
        if verbose and case.state != prior_state:
            print_fn(
                f"[case {case.site} ({case.domain})] request sent: "
                f"{prior_state.value!r} -> {case.state.value!r}"
            )

    # Follow-up pass (issue #68): monitor every already-sent case for a reply, classify it, and --
    # only after explicit human confirmation for a terminal outcome -- transition it. Runs only when
    # both a monitor and a classifier are configured; a case with no new reply is left untouched
    # (R9). Queried *after* the send loop so a case sent in this very pass is also monitored. Note a
    # case only *reaches* a monitored state by having been sent, which requires `recipient_email`,
    # so `follow_up()`'s reply-domain matching always has a real recipient to key on.
    if reply_monitor is not None and response_classifier is not None:
        # Filtered in Python rather than `.where(Case.state.in_(...))` -- the same `ty`-typing
        # limitation already documented a few lines above this function for `eligible_cases`.
        monitored_cases = [
            case
            for case in session.exec(select(Case)).all()
            if case.state in FOLLOWUP_MONITORED_STATES
        ]
        if verbose:
            print_fn(f"follow-up pass: checking {len(monitored_cases)} case(s) for a new reply.")
        for case in monitored_cases:
            # Snapshot of this case's already-recorded `response_classified` Events (issue #98,
            # verbose only) -- taken before follow_up() runs so a genuinely new reply this pass can
            # be told apart from one already classified in an earlier run, purely by comparing
            # Event ids. Never touches follow_up()'s own reply-matching/classification logic.
            prior_classified_ids = (
                {
                    e.id
                    for e in session.exec(
                        select(Event).where(
                            Event.case_id == case.id, Event.kind == "response_classified"
                        )
                    ).all()
                }
                if verbose
                else set()
            )
            try:
                await follow_up(session, case, reply_monitor, response_classifier, notifier)
                session.commit()
                if verbose:
                    new_events = [
                        e
                        for e in session.exec(
                            select(Event).where(
                                Event.case_id == case.id, Event.kind == "response_classified"
                            )
                        ).all()
                        if e.id not in prior_classified_ids
                    ]
                    for event in new_events:
                        intent = event.detail.get("intent", "unclear")
                        print_fn(
                            f"[case {case.site} ({case.domain})] reply found and classified: "
                            f"intent={intent!r}"
                        )
            except Exception:
                # A transport failure (Gmail/LLM) for one case must never abort the whole pass --
                # every other monitored case still deserves its own follow_up() attempt this run.
                # Rolled back so this case's partial, uncommitted changes never leak into the next
                # case's session state; safe to retry next invocation (R9: no deadline is implied
                # by a failed attempt, exactly as a case with no reply yet is left untouched).
                _logger.exception("follow-up pass failed for case %s; will retry next run", case.id)
                session.rollback()
    elif verbose:
        # Real report (issue #98): the follow-up pass silently not running at all (no cached Gmail
        # token yet -- see issue #99, which owns *why* `reply_monitor`/`response_classifier` may be
        # `None`) previously looked identical to it running and simply finding nothing. This line
        # only observes the same `reply_monitor is not None and response_classifier is not None`
        # check the pipeline already used above -- it never changes which branch runs.
        missing = [
            name
            for name, collaborator in (
                ("reply_monitor", reply_monitor),
                ("response_classifier", response_classifier),
            )
            if collaborator is None
        ]
        print_fn(f"follow-up pass skipped this run: {', '.join(missing)} not configured.")


async def _run_with_configured_notifier(
    session: Session,
    discovery_source: DiscoverySource | None,
    target_email: str,
    llm: LLMProvider,
    legal_provider: LegalTemplateProvider,
    global_config: GlobalConfig,
    sender: Sender,
    model: str,
    jurisdiction_detector: JurisdictionDetector | None = None,
    recipient_detector: RecipientDetector | None = None,
    reply_monitor: InboxReplyMonitor | None = None,
    response_classifier: ResponseClassifier | None = None,
    verbose: bool = False,
) -> None:
    """Resolve `RTBF_NOTIFIER_BACKEND` (issue #60) and run one `run_once()` pass against it.

    Split out from `main_async()` so it can be exercised in tests against fakes for every
    collaborator `run_once()` needs, without also needing a real Gmail account/LLM vendor/SMTP
    server (those are resolved by `main_async()` itself, before this function is ever reached). The
    actual notifier-backend resolution lives in `_resolve_notifier_and_run()`.

    `discovery_source` is forwarded to `run_once()` as-is, including `None` (issue #86) --
    `process_main_async()` (the `process` shell command) is the caller that passes
    `None`; `main_async()`/`scan-inbox` still always passes a real Gmail one.

    `model` (required, no default) is forwarded to `run_once()` as-is -- see that function's
    docstring.

    `verbose` (issue #98) is forwarded to `run_once()` as-is; defaults to `False`, unchanged output.
    """

    async def _action(notifier: Notifier) -> None:
        await run_once(
            session,
            discovery_source,
            target_email,
            llm,
            notifier,
            legal_provider,
            global_config,
            sender,
            model=model,
            jurisdiction_detector=jurisdiction_detector,
            recipient_detector=recipient_detector,
            reply_monitor=reply_monitor,
            response_classifier=response_classifier,
            verbose=verbose,
        )

    await _resolve_notifier_and_run(_action)


async def _resolve_notifier_and_run(action: Callable[[Notifier], Awaitable[None]]) -> None:
    """Resolve `RTBF_NOTIFIER_BACKEND` (issue #60) and run `action(notifier)` against it.

    `"console"` (the default): builds a bare `ConsoleNotifier()`. `"telegram"`: builds a
    `TelegramNotifier`. Neither needs any startup/teardown lifecycle anymore -- `notify()` is a
    single outbound call, not something that waits on an incoming update (see `TelegramNotifier`'s
    module docstring for why the old `Application`-polling dance existed and was removed).
    """
    backend = _resolve_notifier_backend()
    if backend == "telegram":
        await action(_build_telegram_notifier())
        return

    await action(ConsoleNotifier())


def _build_jurisdiction_detector(llm: LLMProvider) -> JurisdictionDetector:
    """Build a real `JurisdictionDetector` (issue #63) -- always returns one, regardless of
    whether SearXNG is configured.

    Crawling the site itself (real report: a user adding a case already gives the agent the
    site's own URL and expects it to navigate *that site*, not search the web -- see
    `JurisdictionDetector`'s "crawl first" class docstring section) needs no external service at
    all, just `HTTPWebFetchProvider` (unconditional, below). SearXNG is only an optional add-on
    for the keyword-search *fallback* path now, not a prerequisite for detection to run at all --
    `resolve_searxng_config()` raising `LookupError` (the common case for anyone who hasn't set up
    a SearXNG instance) just means `websearch=None`, not "return `None`, detection entirely off"
    the way it used to. Only a genuine misconfiguration of an *otherwise-present* `[websearch]`
    section (e.g. invalid TOML, a non-string `base_url`) still propagates as `ValueError` -- the
    same "fail loud on a config the operator clearly meant to set up wrong" policy
    `resolve_searxng_config()`'s own docstring documents.
    """
    websearch: WebSearchProvider | None
    try:
        websearch = SearXNGProvider(resolve_searxng_config())
    except LookupError:
        websearch = None

    llm_config = resolve_llm_config()
    return JurisdictionDetector(
        websearch,
        llm,
        model=llm_config.model,
        webfetch=HTTPWebFetchProvider(),
    )


def _build_recipient_detector(llm: LLMProvider) -> RecipientDetector:
    """Build a real `RecipientDetector` (issue #64) -- the exact sibling of
    `_build_jurisdiction_detector()` (they share the same `[websearch]`/`RTBF_SEARXNG_*` config --
    one search backend, both detectors); see that function's docstring for the full rationale for
    why this always returns a detector now instead of `None`.
    """
    websearch: WebSearchProvider | None
    try:
        websearch = SearXNGProvider(resolve_searxng_config())
    except LookupError:
        websearch = None

    llm_config = resolve_llm_config()
    return RecipientDetector(
        websearch,
        llm,
        model=llm_config.model,
        webfetch=HTTPWebFetchProvider(),
    )


def _build_response_classifier(llm: LLMProvider) -> ResponseClassifier:
    """Build the reply-intent `ResponseClassifier` (issue #68) over the resolved `LLMProvider`.

    Unlike the jurisdiction/recipient detectors, follow-up classification needs no web-search
    backend -- it reads the actual text of a reply, not researched facts about a site -- so it is
    always buildable whenever an LLM is configured (which every real run already requires), never
    gated on optional `[websearch]` config.
    """
    llm_config = resolve_llm_config()
    return ResponseClassifier(llm, model=llm_config.model)


def _build_gmail_reply_monitor() -> InboxReplyMonitor:
    """Build the follow-up `InboxReplyMonitor` over a read-only Gmail reply reader (issue #68).

    Reuses the exact same read-only Gmail credentials as inbox-scan discovery
    (`_resolve_gmail_credentials()`) -- monitoring replies introduces no new Gmail scope or write
    access. Lazily imports `googleapiclient` (via `GmailReplyReader`'s own edge import) and the
    Google client builder for the same real-run-only reason the discovery source does.
    """
    from googleapiclient.discovery import build

    from rtbf_agent.plugins.followup.gmail_reader import GmailReplyReader

    resource = build("gmail", "v1", credentials=_resolve_gmail_credentials())
    return InboxReplyMonitor(GmailReplyReader(resource))


def _build_imap_reply_monitor() -> InboxReplyMonitor:
    """Build the follow-up `InboxReplyMonitor` over a real, password-authenticated
    `SubprocessImapReplyReader` (issue #102) -- the Gmail-OAuth-free alternative to
    `_build_gmail_reply_monitor()`.

    `resolve_imap_config()` raises `LookupError`/`ValueError` naming exactly what's missing/invalid
    if `RTBF_IMAP_*` isn't fully configured -- propagated as-is rather than caught here, mirroring
    how `_build_telegram_notifier()` lets `resolve_telegram_config()`'s errors propagate. Lazily
    imports both the config resolver and the reader so this module stays importable without ever
    touching `imaplib`/`multiprocessing` unless the `"imap"` backend is actually selected.

    `SubprocessImapReplyReader`, not the plain `ImapReplyReader` it wraps (real report): a live test
    against a genuine Gmail IMAP account hung completely, for minutes, inside a single blocking
    `imaplib` call -- past every thread-based timeout this codebase tried, because the underlying
    hang appears to hold the GIL the whole time, starving even the event loop's own timeout
    callbacks. Running the actual IMAP work in a forcibly-killable child process is the only
    mechanism that recovers from that regardless of what the blocking call is stuck doing -- see
    `plugins/followup/imap_reader.py`'s module docstring for the full reasoning.
    """
    from rtbf_agent.plugins.followup.imap_config import resolve_imap_config
    from rtbf_agent.plugins.followup.imap_reader import SubprocessImapReplyReader

    imap_config = resolve_imap_config()
    reader = SubprocessImapReplyReader(
        host=imap_config.host,
        port=imap_config.port,
        username=imap_config.username,
        password=imap_config.password,
    )
    return InboxReplyMonitor(reader)


def _build_reply_monitor() -> InboxReplyMonitor:
    """Build the follow-up `InboxReplyMonitor` (issue #68), over whichever `MailboxReader` backend
    `RTBF_REPLY_MONITOR_BACKEND` (issue #102) selects.

    `"gmail"` (the default -- unset behaves byte-for-byte identically to before this issue existed)
    builds `GmailReplyReader` over the same read-only OAuth credentials discovery uses; `"imap"`
    builds `ImapReplyReader` over password credentials from `resolve_imap_config()`, with no OAuth
    handshake at all. Mirrors `_resolve_notifier_backend()`/`_build_telegram_notifier()`'s
    established "env-var-selected provider" wiring pattern (issue #60).
    """
    backend = _resolve_reply_monitor_backend()
    if backend == "imap":
        return _build_imap_reply_monitor()
    return _build_gmail_reply_monitor()


async def main_async() -> None:
    engine = build_engine()

    target_email = _resolve_target_email()
    discovery_source = _build_gmail_discovery_source()
    llm = _build_llm_provider()
    # Resolved separately from _build_llm_provider() (which discards the model name once the
    # adapter is built) purely for its `.model` -- the same "resolve twice, cheap, no I/O beyond
    # env/file reads" pattern _build_jurisdiction_detector()/_build_response_classifier() already
    # use for the exact same reason (issue #86's process_main_async() follow-up; real report).
    llm_config = resolve_llm_config()
    legal_provider = RegistryLegalTemplateProvider()
    global_config = resolve_global_config()
    sender = SMTPSender()
    jurisdiction_detector = _build_jurisdiction_detector(llm)
    recipient_detector = _build_recipient_detector(llm)
    # Follow-up monitoring (issue #68) reuses the same read-only Gmail credentials as discovery by
    # default, or a password-authenticated IMAP mailbox if RTBF_REPLY_MONITOR_BACKEND=imap (issue
    # #102) -- _build_reply_monitor() resolves which. The classifier needs only the already-resolved
    # LLM. Both are always built for a real run -- unlike the web-search detectors, neither is gated
    # on optional config.
    reply_monitor = _build_reply_monitor()
    response_classifier = _build_response_classifier(llm)

    with Session(engine) as session:
        await _run_with_configured_notifier(
            session,
            discovery_source,
            target_email,
            llm,
            legal_provider,
            global_config,
            sender,
            llm_config.model,
            jurisdiction_detector,
            recipient_detector,
            reply_monitor,
            response_classifier,
        )


def _looks_like_unknown_model_error(exc: Exception) -> bool:
    """True if `exc` looks like a provider rejected an unrecognized model name.

    `registry.PROVIDER_REGISTRY`'s `models` tuples are a suggestion list, not an allowlist
    enforced anywhere -- they can lag behind a provider's actual catalog (new models added
    provider-side after this build shipped, or before this repo's registry is updated). A
    404 mentioning "model" is the two adapters' shared shape for that rejection (see
    `openai_compatible.py`/`anthropic.py`'s `RuntimeError(f"API returned {status}: ...")`).
    """
    message = str(exc).lower()
    return "404" in message and "model" in message


async def process_main_async(*, verbose: bool = False) -> None:
    """Wire real collaborators for the `process` shell command (issue #86).

    `main_async()`'s sibling with Gmail *discovery* omitted -- no Gmail scan, so a `Case` added
    manually (the shell's `add`, #66/#82) is classified, drafted, recipient-confirmed,
    and sent through the exact same approval-gated `run_once()` pass `scan-inbox` uses for a
    Gmail-discovered one, just without ever scanning Gmail for new accounts. Passing
    `discovery_source=None` here is what makes that possible; see `run_once()`'s docstring for the
    full "why omitting it is safe" rationale.

    Follow-up monitoring (issue #68), unlike discovery, *is* still attempted here -- automated-
    case-loop rework, since `process` is this repo's primary unattended entrypoint (`forget serve`
    calls it in a loop) and a case sitting in `Awaiting response` needs its replies checked
    regardless of how it was discovered. Which mailbox backend it's attempted against is
    `RTBF_REPLY_MONITOR_BACKEND` (issue #102):

    - `"imap"`: attempted unconditionally, with no gate at all. Plain IMAP login needs no
      interactive handshake (unlike Gmail's OAuth), so there is nothing that could hang an
      unattended process here -- `resolve_imap_config()`'s errors on incomplete/invalid config
      propagate loud instead, since an operator who explicitly opted into this backend deserves an
      error naming what's missing, not a silently-skipped pass.
    - `"gmail"` (the default): only attempted at all if `RTBF_GMAIL_CLIENT_SECRETS_FILE` is set --
      unset means Gmail isn't configured for this install at all (the same signal `main_async()`'s
      hard requirement on it already treats as authoritative), so this function genuinely touches
      nothing Gmail-related in that case, exactly as issue #86 requires. When it is set,
      `_gmail_credentials_available_noninteractively()` decides whether follow-up is possible *this
      pass* without ever risking an interactive OAuth handshake hanging an unattended process with
      no TTY: if a cached, still-valid-or-refreshable token exists, `reply_monitor`/
      `response_classifier` are built exactly as `main_async()` builds them; if not, both stay
      `None` and `run_once()` simply skips follow-up this pass (R9 -- no crash, no hang, just no
      monitoring collaborator this time) -- but, unlike before issue #99, this is now logged
      (`_logger.warning`) every time it happens, precisely because "no reply detected" and
      "follow-up never ran" used to be silently indistinguishable to an operator watching a case
      sit in `Awaiting response` forever. A human running `scan-inbox` once establishes the cached
      token every subsequent unattended `process`/`forget serve` pass then reuses.

    Anyone who *also* wants automatic Gmail discovery keeps using `scan-inbox` (`main_async()`,
    unchanged) -- this function is additive, not a replacement.

    `verbose` (issue #98, keyword-only, default `False`) is forwarded to `run_once()` as-is (via
    `_run_with_configured_notifier()`) -- purely additive terminal output, no pipeline behavior
    change; see `run_once()`'s docstring for exactly what it prints.
    """
    engine = build_engine()

    target_email = _resolve_target_email()
    llm = _build_llm_provider()
    llm_config = resolve_llm_config()  # see main_async()'s identical comment for why
    legal_provider = RegistryLegalTemplateProvider()
    global_config = resolve_global_config()
    sender = SMTPSender()
    jurisdiction_detector = _build_jurisdiction_detector(llm)
    recipient_detector = _build_recipient_detector(llm)

    reply_monitor: InboxReplyMonitor | None = None
    response_classifier: ResponseClassifier | None = None
    if _resolve_reply_monitor_backend() == "imap":
        # Unlike Gmail, IMAP needs no interactive OAuth handshake -- a username/password login
        # never blocks on user interaction, so there is no noninteractive-availability gate to
        # check here (issue #102): attempt it unconditionally once `RTBF_REPLY_MONITOR_BACKEND=imap`
        # is selected. `resolve_imap_config()`'s `LookupError`/`ValueError` on incomplete/invalid
        # config propagates loud rather than being silently swallowed -- an operator who explicitly
        # opted into the IMAP backend deserves an error naming exactly what's missing, not a
        # silently-skipped follow-up pass.
        reply_monitor = _build_reply_monitor()
        response_classifier = _build_response_classifier(llm)
    elif os.environ.get("RTBF_GMAIL_CLIENT_SECRETS_FILE"):
        if _gmail_credentials_available_noninteractively():
            reply_monitor = _build_reply_monitor()
            response_classifier = _build_response_classifier(llm)
        else:
            # Real report (issue #99): a case sitting in `Awaiting response` with a genuine reply
            # already in the mailbox stayed stuck forever -- indistinguishable, from the outside,
            # from "monitoring ran and found nothing" -- because Gmail is configured but no usable
            # cached OAuth token exists yet (a human has never run `scan-inbox` to establish one),
            # so this pass silently skips follow-up entirely, with previously no signal anywhere
            # that it did. Still R9-safe (no hang, no crash, no wrong auto-action) -- but silent
            # degradation on every single unattended pass, indefinitely, is itself the bug this
            # logs against. `_logger.warning` rather than `print_fn`: this must surface on every
            # affected pass regardless of any `--verbose` flag (issue #98, separate/unrelated --
            # not touched here), the same way the follow-up/additional-info exception handlers in
            # `run_once()` already use `_logger` for "this pass silently didn't do what it
            # normally would."
            _logger.warning(
                "follow-up monitoring skipped this pass: RTBF_GMAIL_CLIENT_SECRETS_FILE is set "
                "but no usable cached Gmail OAuth token exists yet at RTBF_GMAIL_TOKEN_FILE -- "
                "any case in 'Awaiting response' will not be checked for replies until a human "
                "runs `scan-inbox` once (interactively) to establish one"
            )

    with Session(engine) as session:
        await _run_with_configured_notifier(
            session,
            None,
            target_email,
            llm,
            legal_provider,
            global_config,
            sender,
            llm_config.model,
            jurisdiction_detector,
            recipient_detector,
            reply_monitor,
            response_classifier,
            verbose=verbose,
        )


_DEFAULT_SERVE_INTERVAL_MINUTES = 20.0


def _resolve_serve_interval_minutes(cli_interval_minutes: float | None) -> float:
    """Resolve `forget serve`'s polling interval: `--interval-minutes` (if given) wins, else
    `RTBF_SERVE_INTERVAL_MINUTES`, else `_DEFAULT_SERVE_INTERVAL_MINUTES` (20) -- the same
    "explicit override, then env var, then a hardcoded default" precedence
    `config.py::resolve_global_config()`'s `_resolved()` already establishes elsewhere in this
    repo, just for a single scalar rather than a whole config file. Raises `ValueError` (naming the
    actual bad value) for a non-numeric or non-positive `RTBF_SERVE_INTERVAL_MINUTES` -- a silent
    fallback to the default on a typo would leave an operator who thinks they configured a faster
    interval with the default 20 minutes and no indication anything was wrong. `--interval-minutes`
    gets the identical non-positive check (OCR review finding): `0` would busy-loop
    (`asyncio.sleep(0)` every iteration) and a negative value would raise a much less clear
    `ValueError` from `asyncio.sleep()` itself instead of this function's own clear message.
    """
    if cli_interval_minutes is not None:
        if cli_interval_minutes <= 0:
            raise ValueError(
                f"invalid --interval-minutes {cli_interval_minutes!r}: must be a positive number"
            )
        return cli_interval_minutes
    raw = os.environ.get("RTBF_SERVE_INTERVAL_MINUTES")
    if not raw:
        return _DEFAULT_SERVE_INTERVAL_MINUTES
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"invalid RTBF_SERVE_INTERVAL_MINUTES {raw!r}: must be a positive number"
        ) from exc
    if interval <= 0:
        raise ValueError(f"invalid RTBF_SERVE_INTERVAL_MINUTES {raw!r}: must be a positive number")
    return interval


async def serve_main_async(
    interval_minutes: float | None = None,
    *,
    print_fn: Callable[[str], None] = print,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    run_once_fn: Callable[[], Awaitable[None]] | None = None,
    verbose: bool = False,
) -> None:
    """The recurring loop (automated-case-loop rework): `run_once_fn()`, sleep, repeat, forever.

    This is the primary, day-to-day way this repo is meant to run -- `process_main_async()`
    (`run_once_fn`'s default) already advances every case exactly as far as a recorded human
    decision allows and skips whatever isn't ready yet (R9), so repeating it on an interval is the
    whole "automated" in "automated case loop": a human only ever needs to act (approve a draft,
    confirm a recipient/follow-up, supply requested info) via kanban or the CLI's decision
    subcommands, never re-run anything by hand. A one-off "run it right now, don't wait for the
    interval" pass is still just `forget process` (unchanged) -- this function adds nothing new to
    the pipeline itself, only the loop-and-wait around it.

    Interval resolution (`_resolve_serve_interval_minutes()`): `interval_minutes` (from
    `--interval-minutes`) if given, else `RTBF_SERVE_INTERVAL_MINUTES`, else 20 minutes.

    A single pass raising (a collaborator failure -- LLM/SMTP/Gmail transport error, misconfigured
    env var, etc.) must never kill this process or silently stop advancing every *other* case
    forever after: caught here, logged, and reported via one `notifier.notify()` call, then the
    loop continues to the next interval. The notifier is built once, up front -- independent of
    whatever `run_once_fn()` itself builds internally -- so a failure inside `run_once_fn()` before
    it even reaches its own notifier construction still has something to report through.

    `sleep_fn`/`run_once_fn` are injected (defaulting to real `asyncio.sleep`/`process_main_async`)
    purely so tests can drive a bounded number of iterations instead of an infinite real loop --
    mirrors this repo's established fake-collaborator-injection pattern, not a runtime feature.
    `run_once_fn` defaults to `None`, resolved just below to `process_main_async(verbose=verbose)`
    -- kept a real default value (rather than binding `process_main_async` directly at def time,
    issue #98) so `verbose` reaches the default pass too; an explicitly injected `run_once_fn` (as
    every existing test does) is used exactly as given, `verbose` unused by this function for that
    call (the fake decides its own signature).

    `verbose` (issue #98, default `False`) is forwarded into the default `run_once_fn`'s
    `process_main_async(verbose=...)` call -- purely additive terminal output for each pass, no
    change to the loop-and-sleep shape itself.
    """

    async def _default_run_once_fn() -> None:
        await process_main_async(verbose=verbose)

    resolved_run_once_fn = run_once_fn if run_once_fn is not None else _default_run_once_fn

    resolved_interval = _resolve_serve_interval_minutes(interval_minutes)
    if _resolve_notifier_backend() == "telegram":
        notifier: Notifier = _build_telegram_notifier()
    else:
        notifier = ConsoleNotifier(print_fn=print_fn)
    print_fn(f"forget serve: running every {resolved_interval:g} minute(s) (Ctrl-C to stop)...")
    while True:
        try:
            await resolved_run_once_fn()
        except Exception as exc:
            _logger.exception("forget serve: a pass failed, will retry next interval")
            print_fn(f"forget serve: a pass failed: {exc}")
            try:
                await notifier.notify(
                    NotificationMessage(
                        case_id="serve",
                        text=f"forget serve: a pass failed and will retry next interval: {exc}",
                    )
                )
            except Exception:
                _logger.exception("forget serve: failed to notify about the pass failure above")
        await sleep_fn(resolved_interval * 60)


def _extract_verbose_flag(rest: str) -> tuple[bool, str]:
    """Pull a `--verbose`/`-v` token out of a shell command's `rest` (issue #98), wherever it
    appears among the other whitespace-separated tokens (e.g. `process -v`, `serve --verbose 5`,
    `serve 5 -v`) -- returns `(found, remaining_rest)` with the flag token(s) removed and every
    other token's relative order and spacing otherwise preserved (rejoined with single spaces,
    matching how every other shell command already tokenizes `rest`). Never raises: an absent flag
    just returns `(False, rest)` unchanged.
    """
    tokens = rest.split()
    remaining = [token for token in tokens if token not in ("--verbose", "-v")]
    return len(remaining) != len(tokens), " ".join(remaining)


_SHELL_HELP_TEXT = """\
Available commands:
  setup                          interactive wizard: configure + validate LLM/Telegram/SMTP/
                                  SearXNG/kanban, and start any services those pieces need
  stop                           stop any services 'setup' started (SearXNG, kanban)
  add <url>                      create a Case for a site you already know about, by URL
  process [--verbose|-v]         run one pass right now (classify/draft/confirm/approve/send/
                                  follow-up/resend) over every Case -- no Gmail required;
                                  --verbose prints each meaningful action the pass takes
  serve [interval_minutes] [-v]  run 'process' on a recurring interval, forever (default 20;
                                  Ctrl-C stops it and returns to this prompt); --verbose/-v
                                  prints each meaningful action every pass takes
  scan-inbox                     scan the configured Gmail inbox and run the full
                                  discover/draft/approve/send pass (may open a browser for a
                                  one-time OAuth consent the first time)
  list                           list every Case, grouped by kanban state
  show <id>                      show one Case's full details and Event history
  confirm-recipient <id> <email> set the deletion-request recipient for a Draft-ready case
  set-jurisdiction <id> <value>  review/override a case's applicable jurisdiction (Discovered only)
  set-drafting-language <id> <value>
                                  review/override a case's drafting language (Discovered only)
  approve-draft <id>             approve a case's draft (Draft-ready, or Requires-human-action
                                  with a pending draft) -- the next pass sends it
  reject-draft <id>              reject a case's draft (Draft-ready, or Requires-human-action
                                  with a pending draft) -- final
  confirm-followup <id>          confirm a classified reply's outcome (closes the case)
  dispute-followup <id>          dispute a classified reply's outcome (-> Requires human action)
  reopen-case <id>               undo a wrongly auto-closed Deletion confirmed case
                                  (-> Requires human action)
  provide-text <id> <text>       record a text answer for a case in Requires human action
  draft-additional-info <id>     compose a preview draft from the current round's text/document
                                  answer (case stays Requires human action -- review before
                                  approve-draft/reject-draft/edit-draft)
  edit-draft <id> <text>         hand-edit a Draft-ready (or pending-draft) case's drafted body
  attach-document <id> <path> [label]
                                  upload a document, add it to the vault, and select it for <id>
  list-documents                 list every document in the vault
  select-documents <id> <ids>    select existing vault document(s) for <id>'s round (comma-sep.)
  delete-document <id>           permanently remove a document from the vault
  kanban                         print how to start the web kanban UI
  help                           show this message
  exit / quit                    leave the shell (Ctrl-D also works)
"""


def _render_case_list(session: Session) -> str:
    """Text rendering of every `Case`, grouped by state -- the shell `list` command (issue #82).

    Deliberately the same grouping (`CASE_STATES_IN_ORDER`) and sort (newest `updated_at` first)
    the kanban board (`api/kanban.py`) already uses, just rendered as text instead of HTML -- no
    new query logic, one shared notion of "how cases are grouped/ordered."
    """
    cases = sorted(session.exec(select(Case)).all(), key=lambda case: case.updated_at, reverse=True)
    cases_by_state: dict[CaseState, list[Case]] = {state: [] for state in CASE_STATES_IN_ORDER}
    for case in cases:
        cases_by_state[case.state].append(case)

    lines: list[str] = []
    for state in CASE_STATES_IN_ORDER:
        state_cases = cases_by_state[state]
        if not state_cases:
            continue
        lines.append(f"{state.value}:")
        for case in state_cases:
            lines.append(f"  [{case.id}] {case.site} ({case.domain})")
    if not lines:
        return "No cases yet. Use 'add <url>' to create one."
    return "\n".join(lines)


def _render_case_detail(session: Session, case_id: int) -> str:
    """Text rendering of one `Case`'s fields plus its full `Event` history (issue #82).

    Chronological order (`Event.id` ascending) mirrors `api/case_detail.py`'s existing rationale
    for the same ordering -- see that module's docstring. No new event-detail rendering logic is
    invented: this shows every `detail` key/value pair verbatim, same as that route's
    `_generic_detail_items()` does for non-document events; there is no sensitive raw document
    content to filter here since the database itself never holds any (R5/R6).
    """
    case = session.get(Case, case_id)
    if case is None:
        return f"No case with id {case_id}."

    lines = [
        f"Case [{case.id}] {case.site} ({case.domain})",
        f"  state: {case.state.value}",
        f"  target_email: {case.target_email}",
        f"  applicable_jurisdiction: {case.applicable_jurisdiction}",
        f"  drafting_language: {case.drafting_language}",
        f"  discovered_at: {case.discovered_at.isoformat()}",
        f"  updated_at: {case.updated_at.isoformat()}",
        "",
        "Events:",
    ]
    events = sorted(
        session.exec(select(Event).where(Event.case_id == case.id)).all(),
        key=lambda event: event.id or 0,
    )
    if not events:
        lines.append("  (none)")
    for event in events:
        lines.append(f"  [{event.id}] {event.kind}")
        for key, value in sorted(event.detail.items()):
            lines.append(f"      {key}: {value}")
    return "\n".join(lines)


_MAX_ATTACH_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10 MiB -- mirrors api/document.py's own limit.
"""Upper bound on a locally-attached document's size (OCR review finding): an unbounded
`read_bytes()` on an accidentally-huge file would raise an unclear `MemoryError`/OOM deep inside
`document_store.store_document()`'s encryption step rather than a clear, early error here."""


def _read_document_file(path: str) -> tuple[bytes, str]:
    """Read a local file for `attach-document`, returning `(content, mime_type)`. Raises
    `ValueError` (a plain, no-dependency exception, same reasoning as `case_actions.py`'s own
    vocabulary) naming the actual problem for a missing or oversized file -- used by the
    interactive shell's `attach-document` command (the standalone `attach-document` subcommand
    this used to also serve was removed once the shell became the only CLI surface)."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ValueError(f"no such file: {path}")
    size = file_path.stat().st_size
    if size > _MAX_ATTACH_DOCUMENT_BYTES:
        raise ValueError(
            f"file too large ({size} bytes; maximum is {_MAX_ATTACH_DOCUMENT_BYTES} bytes): {path}"
        )
    content = file_path.read_bytes()
    mime_type, _ = mimetypes.guess_type(str(file_path))
    return content, mime_type or "application/octet-stream"


_KANBAN_HELP_TEXT = """\
The web kanban UI isn't launched from inside this shell (yet). Start it in another terminal with:

  uv run uvicorn rtbf_agent.api:app --reload

Then visit http://127.0.0.1:8000/ -- it reads/writes this same database, so anything you add or
approve there shows up here immediately, and vice versa."""


def shell_main(
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Interactive shell (`forget`, or bare `rtbf-agent`/`python -m rtbf_agent`) -- issue #82,
    superseded by the automated-case-loop rework's design goal below, and since a later maintainer
    decision, the *only* CLI surface this project has at all (see `main()`'s docstring).

    A persistent REPL in the style of `claude`/`opencode`/`hermes`: launch once, then issue short
    commands into it, rather than re-invoking a one-shot process per action.

    **Design goal (revised, automated-case-loop rework): this shell is a first-class, complete
    interface onto `case_actions.py` -- every decision a human can record (confirm a recipient,
    approve/reject a draft, confirm/dispute a follow-up, supply additional info) is a shell
    command, never something you have to leave it to do.** The original, narrower issue #82 design
    (a REPL that could only discover/process/inspect cases, deliberately leaving approve/reject and
    every other decision "out of scope" for later) was a real gap once those decisions became
    first-class standalone subcommands in their own right (`confirm-recipient`, `approve-draft`/
    `reject-draft`, `confirm-followup`/`dispute-followup`, `provide-text`/`attach-document`) -- a
    human landing in this shell (the *default* entrypoint) had no way to reach them without leaving
    it, which defeats the point of a persistent REPL. Every one of those became a shell command
    too, thinly dispatched to the exact same `case_actions` function the kanban web route calls --
    never a second implementation. Once every decision (and every other former standalone
    subcommand) had a shell equivalent, the standalone subcommands themselves were removed
    entirely -- this shell, plus the kanban web UI, is now the only way to drive this project.
    `serve [interval_minutes]` runs the recurring loop right here too; Ctrl-C stops it and returns
    to this prompt rather than exiting the whole shell. `scan-inbox` (the whole-inbox Gmail
    discover/draft/send pass, `main_async()`) also runs directly from here -- the interactive OAuth
    handshake it may need the first time is no different from `setup`'s own OAuth-adjacent flows,
    which already run fine from inside this same shell.

    Still not launched from inside this shell: a managed background kanban server process (`setup`
    starts one for you as a separate process; there's no in-shell equivalent to hop into it).

    One `Session` per command, not one held open for the whole shell lifetime (per the issue's
    explicit design note) -- a long-running shell session must never go stale against concurrent
    writes from the Telegram bot/kanban web UI to the same database. `input_fn`/`print_fn` are
    injectable so tests can drive this without a real terminal.
    """
    engine = build_engine()
    print_fn("rtbf-agent interactive shell. Type 'help' for commands, 'exit' to leave.")
    while True:
        try:
            line = input_fn("rtbf-agent> ").strip()
        except EOFError:
            print_fn("")
            return

        if not line:
            continue
        command, _, rest = line.partition(" ")
        rest = rest.strip()

        if command in ("exit", "quit"):
            return
        if command == "help":
            print_fn(_SHELL_HELP_TEXT)
        elif command == "setup":
            try:
                asyncio.run(run_setup_wizard(input_fn=input_fn, print_fn=print_fn))
            except Exception as exc:
                print_fn(f"error: {exc}")
        elif command == "stop":
            try:
                asyncio.run(stop_services(print_fn=print_fn))
            except Exception as exc:
                print_fn(f"error: {exc}")
        elif command == "kanban":
            print_fn(_KANBAN_HELP_TEXT)
        elif command in ("list", "cases"):
            with Session(engine) as session:
                print_fn(_render_case_list(session))
        elif command == "add":
            if not rest:
                print_fn("usage: add <url>")
                continue
            try:
                target_email = _resolve_target_email()
                # Built fresh per-invocation, not at shell startup -- same lazy pattern
                # `draft-additional-info` uses below for its own LLM collaborator. Issue #63
                # parity follow-up: the shell's `add` now proposes (and auto-applies, no prompt)
                # a jurisdiction too, the same way a Gmail-discovered case does.
                llm = _build_llm_provider()
                jurisdiction_detector = _build_jurisdiction_detector(llm)
                with Session(engine) as session:
                    asyncio.run(
                        add_case_for_url(
                            session,
                            rest,
                            target_email,
                            jurisdiction_detector=jurisdiction_detector,
                            print_fn=print_fn,
                        )
                    )
            except Exception as exc:
                # Broadened from a LookupError-only catch around _resolve_target_email() (see
                # `process`'s identical fix, issue #86 follow-up): a real, unexpected failure
                # anywhere in this command -- not just a missing RTBF_TARGET_EMAIL, but now also
                # the new LLM-provider build above -- must not crash the whole shell either.
                print_fn(f"error: {exc}")
        elif command == "process":
            verbose, _ = _extract_verbose_flag(rest)
            try:
                asyncio.run(process_main_async(verbose=verbose))
            except KeyboardInterrupt:
                # A single `process` pass can run for a while (classify/draft/send/follow-up over
                # a real LLM/SMTP/Gmail connection, potentially across many cases) -- Ctrl-C during
                # it must stop just this command and return to the prompt, not propagate up and
                # crash the whole shell (KeyboardInterrupt is a BaseException, so the broad `except
                # Exception` below never would have caught it on its own). Mirrors `scan-inbox`'s/
                # `serve`'s identical handling.
                print_fn("")
                print_fn("process: interrupted")
            except Exception as exc:
                # Broader than `add`'s LookupError-only catch (issue #86 follow-up, real report):
                # `process` runs classify/draft/send for real, over an LLM/SMTP connection --
                # a transient network failure, a misconfigured model name, an SMTP auth error, or
                # any other real-world failure must be reported and leave the shell running, not
                # crash the whole REPL with a raw traceback. Mirrors `follow_up()`'s existing
                # per-case "log and keep going" policy in `run_once()` (R9: a failed attempt here
                # is retried on the next `process`, not treated as a deadline).
                print_fn(f"error: {exc}")
                if _looks_like_unknown_model_error(exc):
                    print_fn(
                        "hint: this can happen when the configured model isn't in this "
                        "build's list of known models for the provider yet -- double-check "
                        "RTBF_LLM_MODEL against your provider's current model names."
                    )
        elif command == "scan-inbox":
            # The whole-inbox pass (issue #82's pre-shell default, `main_async()`, unchanged) --
            # the one former standalone subcommand with no pre-existing shell equivalent (every
            # other one already had a same-named or `add-case`->`add` shell command). Calling
            # `main_async()` directly here, exactly the way `process`/`serve` already call
            # `process_main_async()`/`serve_main_async()` inline, is what closes that gap now that
            # the shell is the only CLI surface -- the interactive Gmail OAuth handshake this may
            # trigger the first time is no different from `setup`'s own OAuth-adjacent flows, which
            # already run fine from inside this shell.
            try:
                asyncio.run(main_async())
            except KeyboardInterrupt:
                # OCR review finding: `main_async()` can run for a while (a real Gmail scan plus a
                # full classify/draft/send/follow-up pass) -- Ctrl-C during it must stop just this
                # command and return to the prompt, the same as `serve`'s own KeyboardInterrupt
                # handling below, not propagate up and crash the whole shell (KeyboardInterrupt is
                # a BaseException, so the broad `except Exception` below never would have caught
                # it on its own).
                print_fn("")
                print_fn("scan-inbox: interrupted")
            except Exception as exc:
                # Same broad catch as `process`'s identical fix above, for the same reason: a real
                # transport failure (Gmail/LLM/SMTP) here must be reported and leave the shell
                # running, not crash the whole REPL with a raw traceback.
                print_fn(f"error: {exc}")
                if _looks_like_unknown_model_error(exc):
                    print_fn(
                        "hint: this can happen when the configured model isn't in this "
                        "build's list of known models for the provider yet -- double-check "
                        "RTBF_LLM_MODEL against your provider's current model names."
                    )
        elif command == "serve":
            verbose, rest_without_flag = _extract_verbose_flag(rest)
            interval_minutes = None
            if rest_without_flag:
                try:
                    interval_minutes = float(rest_without_flag)
                except ValueError:
                    print_fn("usage: serve [interval_minutes] [--verbose|-v]")
                    continue
            try:
                asyncio.run(serve_main_async(interval_minutes, print_fn=print_fn, verbose=verbose))
            except KeyboardInterrupt:
                # Ctrl-C stops the recurring loop and returns to this prompt -- it must not also
                # kill the shell itself, unlike a real Ctrl-C at a bare `forget serve` invocation.
                print_fn("")
                print_fn("serve: stopped")
            except Exception as exc:
                print_fn(f"error: {exc}")
        elif command == "show":
            if not rest.isdigit():
                print_fn("usage: show <case_id>")
                continue
            with Session(engine) as session:
                print_fn(_render_case_detail(session, int(rest)))
        elif command == "confirm-recipient":
            case_id_str, _, email = rest.partition(" ")
            if not case_id_str.isdigit() or not email.strip():
                print_fn("usage: confirm-recipient <case_id> <email>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.confirm_recipient(session, int(case_id_str), email.strip())
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {case_id_str}: recipient confirmed ({email.strip()})")
        elif command == "set-jurisdiction":
            case_id_str, _, jurisdiction = rest.partition(" ")
            if not case_id_str.isdigit() or not jurisdiction.strip():
                print_fn("usage: set-jurisdiction <case_id> <value>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.set_case_jurisdiction(
                        session, int(case_id_str), jurisdiction.strip()
                    )
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {case_id_str}: applicable jurisdiction set ({jurisdiction.strip()})")
        elif command == "set-drafting-language":
            case_id_str, _, drafting_language = rest.partition(" ")
            if not case_id_str.isdigit() or not drafting_language.strip():
                print_fn("usage: set-drafting-language <case_id> <value>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.set_case_drafting_language(
                        session, int(case_id_str), drafting_language.strip()
                    )
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {case_id_str}: drafting language set ({drafting_language.strip()})")
        elif command == "approve-draft":
            if not rest.isdigit():
                print_fn("usage: approve-draft <case_id>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.approve_draft(session, int(rest))
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {rest}: draft approved -- the next automated pass will send it")
        elif command == "reject-draft":
            if not rest.isdigit():
                print_fn("usage: reject-draft <case_id>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.reject_draft(session, int(rest))
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {rest}: draft rejected")
        elif command == "confirm-followup":
            if not rest.isdigit():
                print_fn("usage: confirm-followup <case_id>")
                continue
            with Session(engine) as session:
                try:
                    case = case_actions.confirm_followup(session, int(rest))
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
                print_fn(f"case {rest}: follow-up confirmed -- now {case.state.value!r}")
        elif command == "dispute-followup":
            if not rest.isdigit():
                print_fn("usage: dispute-followup <case_id>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.dispute_followup(session, int(rest))
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {rest}: follow-up disputed -- now 'Requires human action'")
        elif command == "reopen-case":
            if not rest.isdigit():
                print_fn("usage: reopen-case <case_id>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.reopen_case(session, int(rest))
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {rest}: reopened -- now 'Requires human action'")
        elif command == "provide-text":
            case_id_str, _, text = rest.partition(" ")
            if not case_id_str.isdigit() or not text.strip():
                print_fn("usage: provide-text <case_id> <text>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.provide_text_response(session, int(case_id_str), text)
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(
                f"case {case_id_str}: text response recorded -- "
                f"run 'draft-additional-info {case_id_str}' when ready"
            )
        elif command == "draft-additional-info":
            if not rest.isdigit():
                print_fn("usage: draft-additional-info <case_id>")
                continue
            try:
                llm = _build_llm_provider()
                llm_config = resolve_llm_config()
                with Session(engine) as session:
                    case = session.get(Case, int(rest))
                    if case is None:
                        print_fn(f"error: case {rest} not found")
                        continue
                    asyncio.run(
                        draft_additional_info_response(session, case, llm, model=llm_config.model)
                    )
                    session.commit()
            except Exception as exc:
                # Broadened from a ValueError-only catch (OCR review finding on this PR): unlike
                # `edit-draft`/`provide-text`'s plain `case_actions` calls, this command makes a
                # real LLM call over the network to compose the draft body -- a transient failure
                # there (or in the `session.commit()` that follows) must be reported and leave the
                # shell running, not crash the whole REPL with a raw traceback. Same reasoning as
                # `add`/`process`/`serve`'s identical broadening (issue #86 follow-up).
                print_fn(f"error: {exc}")
                continue
            print_fn(
                f"case {rest}: draft composed -- review it (show/kanban), then "
                "approve-draft/reject-draft/edit-draft"
            )
        elif command == "edit-draft":
            case_id_str, _, text = rest.partition(" ")
            if not case_id_str.isdigit() or not text.strip():
                print_fn("usage: edit-draft <case_id> <text>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.edit_draft(session, int(case_id_str), text)
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"case {case_id_str}: draft updated")
        elif command == "attach-document":
            case_id_str, _, remainder = rest.partition(" ")
            # `shlex.split()`, not a second `partition(" ")` (OCR review finding): a plain
            # partition truncates a <path> containing spaces at its first space, misreading the
            # rest of the path as the label -- shlex requires quoting a path with spaces
            # (`attach-document 1 "my document.pdf" "ID card"`) but never silently mangles one.
            try:
                parts = shlex.split(remainder)
            except ValueError as exc:
                print_fn(f"error: {exc}")
                continue
            if not case_id_str.isdigit() or not parts:
                print_fn("usage: attach-document <case_id> <path> [label]")
                continue
            path = parts[0]
            label = " ".join(parts[1:]) if len(parts) > 1 else None
            try:
                content, mime_type = _read_document_file(path)
            except ValueError as exc:
                print_fn(f"error: {exc}")
                continue
            with Session(engine) as session:
                try:
                    case_actions.attach_document(
                        session, int(case_id_str), content, mime_type, label=label
                    )
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(
                f"case {case_id_str}: document attached -- "
                f"run 'draft-additional-info {case_id_str}' when ready"
            )
        elif command == "list-documents":
            with Session(engine) as session:
                documents = case_actions.list_vault_documents(session)
            if not documents:
                print_fn("vault is empty")
                continue
            for document in documents:
                doc_label = document.label or "(untitled)"
                print_fn(
                    f"{document.id}\t{doc_label}\t{document.mime_type}\t{document.size} bytes\t"
                    f"{document.received_at.isoformat()}"
                )
        elif command == "select-documents":
            case_id_str, _, ids = rest.partition(" ")
            if not case_id_str.isdigit() or not ids.strip():
                print_fn("usage: select-documents <case_id> <id1,id2,...>")
                continue
            try:
                document_ids = [int(part.strip()) for part in ids.split(",") if part.strip()]
            except ValueError:
                print_fn(f"error: not a comma-separated list of integers: {ids!r}")
                continue
            with Session(engine) as session:
                try:
                    case_actions.select_documents_for_resend(
                        session, int(case_id_str), document_ids
                    )
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(
                f"case {case_id_str}: {len(document_ids)} document(s) selected -- "
                f"run 'draft-additional-info {case_id_str}' when ready"
            )
        elif command == "delete-document":
            if not rest.isdigit():
                print_fn("usage: delete-document <document_id>")
                continue
            with Session(engine) as session:
                try:
                    case_actions.delete_vault_document(session, int(rest))
                except CaseActionError as exc:
                    print_fn(f"error: {exc}")
                    continue
            print_fn(f"document {rest}: deleted from the vault")
        else:
            print_fn(f"unknown command: {command!r}. Type 'help' for the list of commands.")


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint. Since a later maintainer decision, the interactive shell (`shell_main()`,
    issue #82) is the *only* CLI surface this project has -- every standalone one-shot subcommand
    this used to dispatch via `argparse` (`process`, `scan-inbox`, `serve`, `add-case`,
    `confirm-recipient`, `approve-draft`/`reject-draft`, `confirm-followup`/`dispute-followup`,
    `reopen-case`, `provide-text`/`attach-document`/`draft-additional-info`/`edit-draft`,
    `list-documents`/`select-documents`/`delete-document`, `set-jurisdiction`/
    `set-drafting-language`, `cases`/`show`, `setup`/`stop`) is gone. Every one of those already had
    a same-named (or, for `add-case`, `add`) shell command doing the exact same thing; `scan-inbox`
    was the one gap, closed by adding it as a shell command too (see `shell_main()`'s docstring).

    `argv` is accepted (and otherwise ignored) purely so existing callers/tests that invoke
    `main([...])` keep working -- there is no subcommand left to parse it for. Run with no
    arguments (`rtbf-agent`, `forget`, or `python -m rtbf_agent`) to start the shell, then type
    `help` inside it for the full command list; that is now the entire interface this function
    has, regardless of what `argv` contains -- including a former subcommand name, or `-h`/
    `--help`, which no longer print anything special and just open the shell like any other argv.
    """
    shell_main()
