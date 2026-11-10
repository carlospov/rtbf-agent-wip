"""Abstract contracts for RTBF-Agent's plugin families.

This module defines *interfaces only* — no concrete logic. Every discovery source, legal
template provider, notification channel, and LLM provider adapter implements one of the ABCs
below and lives under ``plugins/``, never here. See ``docs/planning/RESTRICTIONS.md`` for the
full restriction text and ``docs/planning/REQUIREMENTS_TRACE.md`` for how each restriction below
maps to a functional requirement and a test.

Design precedent for ``LLMProvider`` specifically: ``docs/architecture/llm-provider-patterns.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

# --------------------------------------------------------------------------------------------- #
# Shared value objects
#
# Plain, generic data carriers used by the interfaces below. They hold no jurisdiction-,
# language-, or site-specific *values* — only type-annotated fields a plugin fills in with
# whatever value applies to it. Keeping them here (rather than in a plugin) is what lets a new
# plugin implement an existing contract without ever touching this file (R21).
# --------------------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiscoveredAccount:
    """One account/site a `DiscoverySource` believes is tied to the user's email.

    Fields mirror the subset of `Case` (spec S3.2) that discovery is responsible for populating;
    `core/models.py` is the source of truth for the full `Case` schema.
    """

    site: str
    domain: str
    target_email: str
    evidence: Mapping[str, Any]
    """Machine-checkable evidence for this discovery, e.g. {"email_id": ..., "matched_pattern":
    ...}. Populates `Case.discovery_evidence` (spec S3.2) — never assumed, always recorded."""


@dataclass(frozen=True)
class LegalTemplate:
    """A deletion-request template resolved for one `(jurisdiction, drafting_language)` pair."""

    jurisdiction: str
    drafting_language: str
    legal_basis: str
    body_template: str


class ApprovalDecision(Enum):
    """The only two ways a human can resolve an approval request — no third "auto" outcome."""

    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class NotificationMessage:
    """A one-way notice to the human — no response expected."""

    case_id: str
    text: str


@dataclass(frozen=True)
class DocumentAttachment:
    """A decrypted document's bytes, in memory only, for one `EmailMessage` to carry (issue #69).

    Only ever constructed right before a `Sender.send()` call that's already past the approval
    gate -- never stored, logged, or handed to anything except a `Sender` (R6).
    """

    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class EmailMessage:
    """One outbound deletion-request email, fully rendered and ready to send.

    Deliberately carries only what a `Sender` needs to put bytes on the wire: no `Case`/`Event`
    reference, no jurisdiction/site logic. Whatever plugin renders `subject`/`body` (legal
    template resolution, drafting language, etc.) has already run by the time this value object
    exists; a `Sender` implementation must treat both as opaque text (R20).
    """

    case_id: str
    to_address: str
    subject: str
    body: str
    attachments: tuple[DocumentAttachment, ...] = ()
    """Zero or more requested identity documents (issue #69; issue #111's vault lets more than one
    be selected for a single resend, e.g. an ID card *and* proof of address). `Sender`
    implementations that don't support attachments may raise rather than silently drop them --
    silently sending without a document a human approved attaching would be worse than failing
    loud."""
    message_id: str | None = None
    """The `Message-ID` the caller (`agent_loop._execute_send()`) generated for this send, if any --
    a `Sender` implementation that can set an outgoing `Message-ID` header must set exactly this
    value there (documented, not type-enforced, mirroring `attachment`'s contract above). Exists so
    a later follow-up pass can search for the site's reply by `In-Reply-To`/`References` instead of
    a broad sender-domain guess (see `plugins/followup/monitor.py`'s `find_new_reply()`) -- a real
    report traced a multi-minute hang to exactly that guess over-matching hundreds of unrelated
    messages in a real mailbox. `None` (the default) preserves the old behavior of leaving the
    header to `smtplib`/the outbound MTA, for any `Sender` that doesn't generate one."""
    in_reply_to: str | None = None
    """The `Message-ID` of the message this email is actually replying to (the site's own reply,
    for a resend after an additional-info round), if any -- a `Sender` implementation that can set
    an outgoing `In-Reply-To` header must set exactly this value there. `None` (the default, always
    true for a case's very first send) leaves the header unset, same as today. Real report: every
    send this tool made generated a brand-new `Message-ID` with no `In-Reply-To`/`References` at
    all, so a resend after "we need your ID" looked like a fresh, unrelated email in the recipient's
    mail client instead of landing in the existing conversation -- `agent_loop._execute_send()`
    derives this from the case's own audit trail (`_thread_reference_ids()`)."""
    references: str | None = None
    """The full chain of prior `Message-ID`s in this case's conversation, space-separated oldest
    first (RFC 5322 `References` -- what real mail clients, Gmail included, actually use to group a
    conversation, more reliably than `In-Reply-To` alone across multiple resend rounds). `None` iff
    `in_reply_to` is also `None` -- see its docstring."""


@dataclass(frozen=True)
class SearchResult:
    """One result from a `WebSearchProvider.search` call — provider-agnostic (R16, R20)."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class FetchedPage:
    """One page's extracted plain text from a `WebFetchProvider.fetch` call — provider-agnostic
    (R16, R20). `text` is already extracted/capped by the adapter, never raw HTML/bytes -- a
    caller (e.g. jurisdiction/recipient detection) feeds this straight into an LLM prompt
    alongside a `SearchResult`'s snippet, not the other way around: search finds candidate pages,
    fetch reads one."""

    url: str
    text: str
    links: tuple[str, ...] = ()
    """Absolute same-page hyperlink targets the adapter found on this page (empty for a
    non-HTML fetch, or if the concrete adapter doesn't support link extraction). Lets a caller
    (e.g. `plugins/webfetch/crawl.crawl_site()`) navigate outward from a page it already fetched
    -- e.g. following a homepage's footer links to find a site's own privacy/contact page --
    without this interface knowing anything about *what* a caller does with them (R20)."""


@dataclass(frozen=True)
class LLMMessage:
    """One protocol-agnostic message in an `LLMRequest`."""

    role: str
    content: str


@dataclass(frozen=True)
class LLMRequest:
    """Everything one `LLMProvider.complete` call needs — protocol-agnostic (R16, R19).

    Callers (classify/draft/followup code) are responsible for populating `messages` with only
    the minimum fields the task needs (R19); this type places no restriction on that — it just
    carries whatever the caller decided to send.
    """

    messages: Sequence[LLMMessage]
    model: str
    max_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        # `Sequence` signals read-only intent, but a `list` argument stays externally mutable
        # despite `frozen=True` (only the field *reference* is protected, not a list's contents).
        # Coerce to `tuple` so a caller can't mutate `messages` out from under a request that's
        # already in flight (e.g. a shared list reused across concurrent calls).
        if not isinstance(self.messages, tuple):
            object.__setattr__(self, "messages", tuple(self.messages))


@dataclass(frozen=True)
class LLMResponse:
    """The result of one `complete` call — protocol-agnostic, no vendor-shaped fields."""

    content: str
    model: str
    raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    """Provider-native response payload, kept opaque here so vendor-specific shapes never leak
    into caller code (see `docs/architecture/llm-provider-patterns.md`). Callers must not depend
    on its internal structure; it exists for logging/debugging only. Coerced to `MappingProxyType`
    in `__post_init__` regardless of what's passed in, so the top-level `Mapping` annotation's
    read-only contract holds at runtime for every construction path — not just the default —
    including a concrete adapter passing a plain `dict`. This is a *shallow* guarantee: nested
    mutable values inside `raw` (e.g. a `dict` or `list` value) are not recursively frozen and
    remain mutable in place. That's an accepted tradeoff for a field that exists only for
    logging/debugging, not deep immutability everywhere."""

    def __post_init__(self) -> None:
        if not isinstance(self.raw, MappingProxyType):
            object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


# --------------------------------------------------------------------------------------------- #
# Plugin family contracts
# --------------------------------------------------------------------------------------------- #


class DiscoverySource(ABC):
    """Contract for a plugin that finds accounts tied to a user's email (spec S3.1 "Discovery").

    Enforces:
    - **R20** — no site-specific scraping/matching logic lives in `core/`; only this contract
      does. Concrete sources (inbox scan, breach-database lookup, ...) live under
      `plugins/discovery/`.
    - **R21** — adding a new discovery source (a new inbox provider, a new breach API) requires
      only a new module implementing this ABC; this file never changes for it.
    """

    @abstractmethod
    async def discover(self, target_email: str) -> Sequence[DiscoveredAccount]:
        """Return every account this source believes is tied to `target_email`.

        Must not raise on "nothing found" — return an empty sequence instead. Must not mutate any
        `Case`/`Event` state itself; recording proposals and applying approved state changes is
        the orchestrator's job, not a discovery plugin's (R1, R3).
        """
        raise NotImplementedError


class LegalTemplateProvider(ABC):
    """Contract for a plugin supplying jurisdiction- and language-specific request text.

    Enforces:
    - **R20** — no jurisdiction's legal basis or wording is hardcoded in `core/`; only this
      contract is. Concrete templates (e.g. one jurisdiction's basis in one language) live under
      `plugins/legal_templates/`.
    - **R21** — adding a new jurisdiction or a new drafting language requires only a new module
      implementing this ABC; this file never changes for it.
    - **R24** — `jurisdiction` and `drafting_language` are two independent parameters to
      `get_template`, never a single combined lookup key — so adding a new drafting language for
      an existing jurisdiction is a language-side-only addition.
    """

    @abstractmethod
    def get_template(self, jurisdiction: str, drafting_language: str) -> LegalTemplate:
        """Return the template for this exact `(jurisdiction, drafting_language)` pair.

        Raises `LookupError` (or a subclass) if the plugin has no template for that combination —
        never silently substitutes a different jurisdiction or language.
        """
        raise NotImplementedError

    @abstractmethod
    def supported_jurisdictions(self) -> Sequence[str]:
        """Jurisdictions this provider can produce a template for, independent of language."""
        raise NotImplementedError

    @abstractmethod
    def supported_languages(self, jurisdiction: str) -> Sequence[str]:
        """Drafting languages available for `jurisdiction` specifically."""
        raise NotImplementedError


class Notifier(ABC):
    """`base_notifier` contract (spec S3.1): the only way agent/core code talks to a human.

    **One-way only (automated-case-loop rework):** every decision a human makes (approve/reject a
    draft, confirm a recipient, confirm/dispute a follow-up outcome, supply a text/document answer)
    is recorded through `case_actions.py` -- via the kanban web UI, a CLI subcommand, or (later) a
    Telegram button that calls the same functions directly -- never by a pipeline call blocking on
    this interface. `request_approval()`/`receive_document()`/`receive_text_response()`/
    `download_document_bytes()` used to exist for exactly that blocking wait; they're gone because
    nothing calls them anymore, not because notifying a human about a pending decision stopped
    mattering. The recurring loop (`cli.run_once()`) is the only thing that ever turns a *recorded*
    decision into a real action, and it never needs to ask anything through this interface first.

    Enforces:
    - **R15** — whitelist/allowlist enforcement (e.g. a Telegram `chat_id`) is a concrete
      notifier's responsibility, entirely below this interface; `core/` never sees or reasons
      about channel-specific identities.
    - **R20** — no channel-specific protocol detail (Bot API shapes, chat-app block-kit, ...)
      appears in this contract; only the channel-agnostic notify verb does.
    - **R21** — adding a new channel requires only a new module implementing this ABC under
      `plugins/notifiers/`; this file never changes for it.
    """

    @abstractmethod
    async def notify(self, message: NotificationMessage) -> None:
        """Send a one-way notice. Never returns or implies a decision."""
        raise NotImplementedError


class Sender(ABC):
    """Contract for a plugin that actually dispatches a drafted deletion-request email (spec S3.1).

    Enforces:
    - **R1** — this is the *only* interface that can put a deletion request on the wire, and
      nothing about this contract implies when it's safe to call `send`: that's entirely
      `agent_loop._execute_send()`'s job, which only ever calls it after verifying an already-
      persisted `approval_granted` `Event` for the exact case being sent. A `Sender`
      implementation must not itself solicit or assume approval — it only transmits.
    - **R12/R13** — no centralized backend or telemetry: a concrete `Sender` talks directly to the
      user's own outbound channel (e.g. their own SMTP account), never a project-operated relay.
    - **R14** — credentials a concrete `Sender` needs (SMTP host/username/password, ...) are
      resolved entirely below this interface, from local env vars or a local gitignored config
      file, and must never appear in a `EmailMessage`, a log line, or an `Event.detail` (R4/R5's
      "hash + metadata only" rule extends to any audit trail this action produces).
    - **R21** — adding a new dispatch channel (SMTP, a transactional-email API, ...) requires only
      a new module implementing this ABC under `plugins/senders/`; this file never changes for it.
    """

    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        """Dispatch `message` now. Raises on failure rather than swallowing it.

        Must not catch and suppress a transport-level failure (auth error, connection refused,
        ...) — the caller (`agent_loop._execute_send()`) depends on an exception propagating here
        to avoid recording a `request_sent` `Event` or transitioning `Case.state` for a message
        that was never actually delivered.

        If `message.message_id` is set and this implementation's transport supports setting an
        outgoing `Message-ID` header, it must use exactly that value there -- see
        `EmailMessage.message_id`'s docstring for why. Same for `message.in_reply_to`/
        `message.references`, against `In-Reply-To`/`References` respectively (see their own
        docstrings) -- both are `None` together for any send that isn't a reply.
        """
        raise NotImplementedError


class LLMProvider(ABC):
    """Contract every LLM adapter implements — one narrow method, not vendor-shaped methods.

    Deliberately narrow, mirroring `alibaba/open-code-review`'s (OCR) `LLMClient`: OCR supports
    roughly fifteen LLM vendors behind a single-method interface (`CompletionsWithCtx`) because
    nearly every vendor speaks one of only two wire protocols — Anthropic-shaped or
    OpenAI-compatible — so a 16th vendor is a data-only registry entry, never a new method on the
    interface. See `docs/architecture/llm-provider-patterns.md` for the full analysis. This ABC
    copies that shape on purpose: vendor-specific request/response translation belongs entirely
    inside a concrete adapter implementation (`plugins/llm/*`), never in this contract — adding a
    provider-shaped method here (e.g. one method per vendor, or per protocol) would be exactly the
    leak OCR's design avoids.

    Enforces:
    - **R16** — this is the *only* way `core/` (classification/drafting/reasoning code) talks to
      an LLM; no file under `core/` imports a concrete SDK directly — only modules under
      `plugins/llm/` may, and only there.
    - **R17** — deliberately narrow (one method) so every required provider can implement it
      without adding provider-shaped methods to this ABC.
    - **R18** — this contract carries no default endpoint or credential; configuration resolution
      is entirely a concrete adapter's responsibility, sourced from local config/env only.
    """

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send `request` to the underlying provider and return its response.

        The only method on this interface — callers depend on it exclusively (R16). Concrete
        adapters translate `LLMRequest`/`LLMResponse` to/from their own wire protocol internally;
        that translation must never leak into a caller-visible shape.
        """
        raise NotImplementedError


class WebSearchProvider(ABC):
    """Contract every web-search adapter implements — one narrow method, mirrors `LLMProvider`.

    Prerequisite for `#63` (automatic jurisdiction detection) and `#64` (automatic recipient
    detection): both need to research a discovered site's *current* privacy contact/jurisdiction
    rather than relying on an LLM's possibly-stale training data, and both do that by feeding
    `search` results into an `LLMProvider.complete` call, not by querying a search vendor's API
    shape directly. Deliberately narrow (one method) for the same reason `LLMProvider` is: nearly
    every web-search vendor exposes a keyword-query-in, ranked-results-out shape, so a second or
    third provider is a new `plugins/websearch/` module, never a new method here.

    Enforces:
    - **R16** — this is the *only* way `core/` (and, once built, `#63`/`#64`'s detection logic)
      talks to a web-search backend; no file under `core/` imports a concrete search SDK/HTTP
      client directly — only modules under `plugins/websearch/` may, and only there.
    - **R20** — no vendor-specific request/response shape (query operators, ranking signals,
      pagination tokens, ...) appears in this contract; only the provider-agnostic `search(query)`
      verb and the plain `SearchResult` value object do.
    - **R21** — adding a new search backend (a different API, a self-hosted metasearch instance)
      requires only a new module implementing this ABC under `plugins/websearch/`; this file never
      changes for it.
    """

    @abstractmethod
    async def search(self, query: str) -> Sequence[SearchResult]:
        """Return ranked results for `query`, most relevant first.

        Must not raise on "nothing found" — return an empty sequence instead. Concrete adapters
        translate `query`/results to and from their own wire protocol internally; that translation
        must never leak into a caller-visible shape (R20).
        """
        raise NotImplementedError


class WebFetchProvider(ABC):
    """Contract every page-fetch adapter implements — one narrow method, mirrors
    `WebSearchProvider`.

    `WebSearchProvider.search()` only ever returns a search engine's own indexed title/URL/snippet
    for a page — never the page's actual content. Real report: `RecipientDetector` found a site's
    own contact page in search results, but the contact email lived in the page body, not in the
    engine's indexed snippet, so the LLM never saw it and correctly declined to guess. This
    contract exists so detection can optionally read a candidate page's real content, the same
    "search finds candidates, fetch reads them" two-tool split every general-purpose research
    agent uses (search and fetch are two different capabilities, never one).

    Enforces:
    - **R16** — this is the *only* way `core/`-adjacent detection logic talks to an HTTP-fetch
      backend; no file under `core/` imports a concrete HTTP client directly — only modules under
      `plugins/webfetch/` may, and only there.
    - **R20** — no vendor/backend-specific shape (headers, redirects-followed, encoding, ...)
      appears in this contract; only the provider-agnostic `fetch(url)` verb and the plain
      `FetchedPage` value object do.
    - **R21** — adding a new fetch backend (a different HTTP client, a headless-browser-backed
      renderer for JS-heavy pages) requires only a new module implementing this ABC under
      `plugins/webfetch/`; this file never changes for it.
    """

    @abstractmethod
    async def fetch(self, url: str) -> FetchedPage | None:
        """Fetch `url` and return its extracted plain text, or `None` if it couldn't be fetched
        usefully (a non-2xx status, a non-text content type, a page a concrete adapter's own
        safety checks refuse to fetch, ...).

        Must not raise for those cases -- mirrors `WebSearchProvider.search()`'s "nothing found"
        contract, since a caller fetching several candidate URLs must be able to treat any single
        one of them coming back empty as routine, not exceptional. Raises only for a genuine
        transport failure (DNS/connection error, timeout) -- a caller fetching multiple URLs
        concurrently is expected to catch that per-URL itself and continue with the others (R9's
        "an optional enhancement failing must never crash the run" spirit, same as
        `WebSearchProvider`).
        """
        raise NotImplementedError


class I18nProvider(ABC):
    """Contract for resolving a user-facing string from a translation/resource system.

    Enforces:
    - **R23** — every interface string (bot copy, kanban UI copy) is sourced from this contract,
      never a literal embedded in caller code. This ABC is the *only* sanctioned path from a
      lookup key to displayed text; a Telegram bot and a kanban UI each depend on it instead of
      inventing their own lookup.
    - **R21** — adding a language, or swapping the resource format/storage entirely, requires
      only a new module implementing this ABC under `plugins/i18n/`; this file never changes for
      it.

    Deliberately one method: resolving a key for a language is the entire contract. Formatting
    parameters (pluralization, interpolation, ...) are the concrete implementation's problem, not
    something this interface needs to know the shape of.
    """

    @abstractmethod
    def translate(self, key: str, lang: str, **params: object) -> str:
        """Resolve `key` to display text in `lang`, interpolating `params` into it.

        Raises `LookupError` (or a subclass) if `key` has no entry for `lang` — never falls back
        to returning `key` itself or an empty string. A missing translation must fail loud, not
        silently degrade the text a human sees (R23). May also raise `ValueError` for a corrupted
        or malformed resource (e.g. invalid TOML, a non-string value for `key`) — a distinct
        failure mode from "translation missing", not expected to be handled the same way.
        """
        raise NotImplementedError
