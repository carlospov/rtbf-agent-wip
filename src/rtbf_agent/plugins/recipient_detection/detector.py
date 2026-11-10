"""`RecipientDetector` — compose web search + LLM into a *proposed* recipient contact (issue #64).

The sibling of `plugins/jurisdiction_detection/detector.JurisdictionDetector` (issue #63): where
that module proposes which data-protection *jurisdiction* governs a site, this one proposes the
site's actual privacy/DPO *contact address* (where a deletion request would be emailed). The
architecture is identical on purpose — read that module's docstring for the full rationale; the two
hard boundaries below are the same, and matter *more* here:

1. **This module never writes to a `Case`.** `propose()` returns a plain `RecipientAttempt` value
   object (or raises); it does not touch the database, mutate a `Case`, or record an `Event`.
   Turning a proposal into a persisted `Case.recipient_email` happens only in
   `case_actions.confirm_recipient()` (kanban or CLI), and only *after* a human has seen it —
   explicitly labelled as unverified LLM output — and typed the address themselves. There is no
   code path from a proposal to a stored recipient that skips that confirmation. This is a
   stronger requirement than issue #63's
   jurisdiction proposal: a wrong jurisdiction is just metadata, but a wrong recipient means a real
   deletion request could be emailed to the wrong address on approval (R1). The proposal is only
   ever a *starting suggestion* for the manual entry issue #52 already requires, never a value that
   can reach `Case.recipient_email` without a human keystroke confirming that exact address.
2. **This module is a plugin, not core.** The "search the web, ask the LLM to synthesize a privacy
   contact" logic lives here, behind the injected `WebSearchProvider` + `LLMProvider` contracts
   (R16, R20) — never inline in `core/`, `agent_loop.py`, or `cli.py`. Swapping either backend is a
   plugin swap, not a change here (R21). No per-site contact list is hardcoded (R20): the *upstream
   engines* surface a site's actual privacy-policy contact, and the LLM synthesizes from what comes
   back.

`propose()` distinguishes two non-crash outcomes, and its caller (`cli._propose_recipient()`)
treats both as "no proposal" for the purpose of the required manual entry (issue #52, unchanged),
but records different audit `Event`s for each so the case history can tell them apart:

- **`RecipientAttempt(proposal=None)` — low confidence.** Search returned nothing usable, or the
  LLM couldn't name a contact it was confident about (an empty/`null`/`"UNKNOWN"` answer, or an
  unparseable one). An expected, non-error outcome — does *not* raise; `.query`/`.results` are
  still populated so the caller can log what was searched.
- **an exception — the capability failed.** Web search was unreachable/misconfigured, or the LLM
  transport errored. `propose()` lets those propagate; `cli._propose_recipient()` is where the
  "an optional enhancement failing must never crash the run" policy is applied, so this module
  stays honest about real failures rather than swallowing them and silently degrading with no
  signal.

**Crawl the site first, web search only as a fallback.** Real report: a user adding a case already
gives the agent the site's own URL -- they want the agent to navigate *that site* looking for its
own contact page, not run a keyword search across the whole web and hope a relevant result surfaces
(the `3djuegos.com` bug that motivated `plugins/webfetch/` in the first place was itself a symptom
of the search-first approach: mostly irrelevant results from unrelated companies). When a
`webfetch` collaborator is configured, `propose()` tries `plugins/webfetch/crawl.crawl_site()`
first; only if that finds no usable pages at all does it fall through to the keyword-search flow
below, completely unchanged. `websearch` is accordingly now `WebSearchProvider | None` -- a
detector can be crawl-only (no search backend configured at all) and still fully function.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from rtbf_agent.core.interfaces import (
    FetchedPage,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    SearchResult,
    WebFetchProvider,
    WebSearchProvider,
)
from rtbf_agent.plugins.webfetch.crawl import crawl_site

_logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 6
"""How many top search results to feed the LLM. Enough context to synthesize from, few enough to
keep the prompt small (R19: only the minimum data the task needs)."""

_MAX_SNIPPET_CHARS = 500
"""Per-result snippet cap in the prompt — one runaway upstream snippet shouldn't dominate the
prompt or the token budget."""

_MAX_FETCHED_TEXT_CHARS = 3000
"""Per-result fetched-page-text cap in the prompt, mirroring `_MAX_SNIPPET_CHARS` above.
code-reviewer finding: `WebFetchProvider`'s own contract only promises the text is "already
extracted/capped by the adapter" -- not a specific limit, and not a guarantee every adapter
honors. Capping again here, independent of whatever `HTTPWebFetchProvider._MAX_FETCHED_TEXT_CHARS`
happens to be, means this prompt stays bounded (R19) even against a different `WebFetchProvider`
implementation with a much larger or no cap of its own -- the same reasoning `cli.py`'s
`_search_results_detail()` already applies to a logged snippet rather than trusting it was already
bounded upstream."""

DEFAULT_FETCH_TOP_N = 3
"""How many of the top search results to also fetch full page content for, when a `webfetch`
collaborator is configured -- see the class docstring's "why fetching" rationale. Deliberately
smaller than `DEFAULT_MAX_RESULTS`: fetching is a real HTTP request per result, so only the
results most likely to be the actual site (already ranked first by the search backend) are worth
the extra latency/load."""

_UNKNOWN_MARKER = "UNKNOWN"
"""The sentinel the LLM is instructed to return when it cannot confidently determine a contact —
mapped to a `None` proposal (low confidence), never surfaced as a real recipient."""


def _cap_fetched_page_text(page: FetchedPage) -> FetchedPage:
    """Re-cap `page.text` at `_MAX_FETCHED_TEXT_CHARS` regardless of the source (a search-result
    fetch or a crawled page) -- shared by `_fetch_top_results()` and `_propose_from_crawl()` so
    the "don't trust the adapter's own cap as a contract guarantee" rule (see
    `_MAX_FETCHED_TEXT_CHARS`'s docstring) applies identically to both."""
    return FetchedPage(url=page.url, text=page.text[:_MAX_FETCHED_TEXT_CHARS])


@dataclass(frozen=True)
class RecipientProposal:
    """A *suggested* privacy/DPO contact address plus the reasoning/sources behind it.

    Never an assignment: holds no `Case` reference and is never itself persisted. `cli` records the
    reasoning/sources as a `recipient_proposed` `Event` (audit trail, same spirit as
    `jurisdiction_proposed`) and surfaces `recipient` to the human as an *unverified* suggestion.
    Only the human's typed response via `case_actions.confirm_recipient()` (kanban or CLI) ever
    reaches `Case.recipient_email` — the proposal itself can never be written there automatically.
    """

    recipient: str
    """The proposed contact address (e.g. `"privacy@example.com"`). Always non-empty and not the
    low-confidence sentinel — `propose()` returns a `RecipientAttempt` with `proposal=None` in
    those cases. This is a *proposal*, never verified: a human must confirm it before it is ever
    used."""
    reasoning: str
    """The LLM's short explanation of *why* it proposed this address, recorded for audit so a human
    can judge the suggestion rather than take it on faith."""
    sources: tuple[str, ...]
    """The result URLs the proposal was synthesized from — the evidence trail behind the reasoning.
    A `tuple` (not a `list`) so a frozen `RecipientProposal` can't have its sources mutated in place
    after construction."""


@dataclass(frozen=True)
class RecipientAttempt:
    """Everything `propose()` learned in one call, whether or not it reached a confident proposal.

    The sibling of `jurisdiction_detection.detector.JurisdictionAttempt` — see that class's
    docstring for the full rationale. `propose()` always returns one of these (never a bare
    `None`), so `cli._propose_recipient()` can log the query/results even when `.proposal` is
    `None` (a "couldn't determine" outcome still worth auditing, especially here since a wrong or
    missing recipient guess is the input to a real send). Only the exception path (websearch/LLM
    transport failure) skips this -- `propose()` still raises for that.
    """

    query: str
    """The exact search query `propose()` issued -- part of the audit trail for why a proposal was
    or wasn't reached."""
    results: tuple[SearchResult, ...]
    """The (possibly empty) search results `propose()` fed the LLM, capped in *count* to
    `max_results` -- these are the raw `SearchResult`s from `WebSearchProvider.search()`, not the
    snippet-length-truncated form `_user_prompt()` builds for the LLM call itself. A caller that
    logs `.results` (e.g. `cli._search_results_detail()`) must apply its own snippet cap rather
    than assume these are already bounded in content length."""
    proposal: RecipientProposal | None
    """The confident proposal reached from `results`, or `None` if search returned nothing usable
    or the LLM wasn't confident enough to name a contact."""
    fetched_urls: tuple[str, ...] = ()
    """Which of `results`' URLs were also successfully fetched for full page content (only
    non-empty when a `webfetch` collaborator was configured) -- an audit signal that the LLM saw
    more than just a search snippet for these, not the fetched text itself: a full page is a much
    larger, unbounded blob that has no business living in an audit-log `Event.detail` JSON column
    (the same lesson `cli._search_results_detail()`'s snippet cap already applies to `results`)."""


class RecipientDetector:
    """Composes an injected `WebSearchProvider` + `LLMProvider` into a recipient *proposal*.

    Both collaborators are injected (not constructed here), mirroring every other capability in
    this repo — so this class can be exercised in tests against a fake search backend and a fake
    LLM without a real SearXNG instance or LLM vendor, and so swapping either backend is a wiring
    change at the call site, never a change here (R21).
    """

    def __init__(
        self,
        websearch: WebSearchProvider | None,
        llm: LLMProvider,
        *,
        model: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        webfetch: WebFetchProvider | None = None,
        fetch_top_n: int = DEFAULT_FETCH_TOP_N,
    ) -> None:
        """`websearch` may be `None`: crawling the site itself (see `webfetch` below and the class
        docstring's "crawl first" section) needs no search backend at all, so a caller with only
        page-fetching configured -- no SearXNG -- still gets a fully-working detector, not one that
        silently never proposes anything. `None` only matters if crawling *also* finds nothing;
        `propose()` then has no fallback and reports "couldn't determine", same as ever having
        found zero search results.

        `webfetch` is optional and additive (default `None`): omitting it reproduces this
        class's pre-existing snippet-only, search-only behavior exactly (real report: a search
        snippet alone can miss information -- e.g. a contact email -- that only lives in the page
        body). When set, it's used two ways: `crawl_site()` tries it first (see class docstring),
        and -- only on the fallback search path -- the top `fetch_top_n` results also get their
        page content fetched and fed to the LLM alongside their snippet."""
        self._websearch = websearch
        self._llm = llm
        self._model = model
        self._max_results = max_results
        self._webfetch = webfetch
        self._fetch_top_n = fetch_top_n

    async def propose(self, *, site: str, domain: str) -> RecipientAttempt:
        """Research `domain` and return the full attempt, `.proposal` set if confident.

        Tries crawling the site itself first (if `webfetch` is configured); only falls through to
        the keyword-search flow below if that finds no usable pages at all -- see the class
        docstring's "crawl first" section. `.proposal` is `None` (an expected, non-error outcome)
        when neither approach turns up anything usable, or the LLM can't confidently name a
        contact from what was found -- `.query`/`.results` are still populated so the caller can
        log what was tried. Raises (does not swallow) if web search or the LLM *fails* -- the
        caller decides how to degrade (see the module docstring); there is no `RecipientAttempt`
        to return in that case since the attempt itself didn't complete.
        """
        if self._webfetch is not None:
            crawl_attempt = await self._propose_from_crawl(site=site, domain=domain)
            if crawl_attempt is not None:
                return crawl_attempt

        query = self._build_query(site=site, domain=domain)
        if self._websearch is None:
            # No search backend configured, and crawling (if attempted at all) found nothing --
            # nothing left to try. Not an error: same "couldn't determine" outcome as zero search
            # results, just reached with no query ever actually issued.
            return RecipientAttempt(query="", results=(), proposal=None)
        results = list(await self._websearch.search(query))
        if not results:
            # Nothing to synthesize from -- a legitimate "can't determine", not a failure. The
            # caller falls through to the required manual prompt exactly as if detection were off.
            return RecipientAttempt(query=query, results=(), proposal=None)

        results = results[: self._max_results]
        fetched_pages = await self._fetch_top_results(results)
        request = LLMRequest(
            messages=(
                LLMMessage(role="system", content=self._system_prompt()),
                LLMMessage(
                    role="user",
                    content=self._user_prompt(
                        site=site, domain=domain, results=results, fetched_pages=fetched_pages
                    ),
                ),
            ),
            model=self._model,
        )
        response = await self._llm.complete(request)
        fetched_urls = tuple(fetched_pages)

        proposed = self._parse_response(response.content)
        if proposed is None:
            return RecipientAttempt(
                query=query, results=tuple(results), proposal=None, fetched_urls=fetched_urls
            )
        recipient, reasoning = proposed
        proposal = RecipientProposal(
            recipient=recipient,
            reasoning=reasoning,
            sources=tuple(r.url for r in results if r.url),
        )
        return RecipientAttempt(
            query=query, results=tuple(results), proposal=proposal, fetched_urls=fetched_urls
        )

    async def _propose_from_crawl(self, *, site: str, domain: str) -> RecipientAttempt | None:
        """Try `crawl_site()` on `domain`'s own homepage; returns `None` only if crawling found no
        usable pages at all, signaling `propose()` to fall through to the keyword-search flow.
        Once crawling finds at least one page, this always returns a completed `Attempt` --
        including a `proposal=None` one if the LLM still isn't confident -- rather than also
        trying the search fallback (per the class docstring: crawling the site itself is the
        primary path, not merely tried-before-search on every attempt).
        """
        assert self._webfetch is not None  # only called when set, see propose()
        base_url = f"https://{domain}/"
        pages = await crawl_site(self._webfetch, base_url)
        if not pages:
            return None

        query = f"crawl {base_url}"
        capped_pages = [_cap_fetched_page_text(page) for page in pages]
        results = tuple(
            SearchResult(title=page.url, url=page.url, snippet="") for page in capped_pages
        )
        fetched_pages = {page.url: page for page in capped_pages}

        request = LLMRequest(
            messages=(
                LLMMessage(role="system", content=self._system_prompt()),
                LLMMessage(
                    role="user",
                    content=self._user_prompt(
                        site=site, domain=domain, results=list(results), fetched_pages=fetched_pages
                    ),
                ),
            ),
            model=self._model,
        )
        response = await self._llm.complete(request)
        fetched_urls = tuple(fetched_pages)

        proposed = self._parse_response(response.content)
        if proposed is None:
            return RecipientAttempt(
                query=query, results=results, proposal=None, fetched_urls=fetched_urls
            )
        recipient, reasoning = proposed
        proposal = RecipientProposal(
            recipient=recipient,
            reasoning=reasoning,
            sources=tuple(page.url for page in capped_pages),
        )
        return RecipientAttempt(
            query=query, results=results, proposal=proposal, fetched_urls=fetched_urls
        )

    async def _fetch_top_results(self, results: list[SearchResult]) -> dict[str, FetchedPage]:
        """Fetch full page content for the top `fetch_top_n` of `results`, keyed by URL.

        No-op (returns `{}`) if no `webfetch` collaborator is configured. A single URL's fetch
        failing (an exception, or a `None` "couldn't fetch usefully" result) is never fatal to the
        whole attempt -- it's just omitted from the returned mapping, and `_user_prompt()` falls
        back to that result's snippet alone, same "optional enhancement degrades gracefully"
        policy this repo applies everywhere else (R9's spirit).
        """
        if self._webfetch is None:
            return {}
        candidates = [r for r in results[: self._fetch_top_n] if r.url]
        if not candidates:
            return {}
        pages = await asyncio.gather(
            *(self._webfetch.fetch(r.url) for r in candidates), return_exceptions=True
        )
        fetched: dict[str, FetchedPage] = {}
        for candidate, page in zip(candidates, pages, strict=True):
            if isinstance(page, FetchedPage):
                # Capped here too (not only where `_user_prompt()` slices it for the LLM call):
                # code-reviewer finding -- `WebFetchProvider`'s contract promises text is "already
                # capped by the adapter", no specific limit, no guarantee every adapter honors it.
                # Capping at storage time bounds what this dict ever holds in memory, regardless of
                # which adapter is plugged in -- the prompt-time cap alone only bounded what
                # reached the LLM, not what this method kept around.
                fetched[candidate.url] = _cap_fetched_page_text(page)
            elif page is None:
                # Routine, not an error: the adapter's own "couldn't fetch usefully" signal
                # (non-2xx, non-text content type, etc.) -- logged at the same DEBUG level as the
                # Exception case below for symmetry (code-reviewer finding: previously silent,
                # indistinguishable from "webfetch not configured" without cross-referencing
                # Attempt.results/fetched_urls).
                _logger.debug("Fetch returned no useful content for %s", candidate.url)
            elif isinstance(page, Exception):
                # DEBUG, not a print/warning: this is expected background noise for an optional
                # enhancement (a page can legitimately 404/time out/refuse), never surfaced to the
                # operator -- but code-reviewer correctly noted that with no signal at all, an
                # operator has no way to tell "no useful content" apart from "a real network
                # problem is silently eating every fetch", should that ever be worth investigating.
                # `exc_info=page`: logs the full traceback (matching this repo's established
                # `_logger.exception(...)` convention elsewhere), not just `str(page)`.
                _logger.debug("Failed to fetch %s: %s", candidate.url, page, exc_info=page)
            elif isinstance(page, BaseException):
                # code-reviewer finding (correcting the previous commit's own comment here, which
                # was wrong): `asyncio.gather(return_exceptions=True)` does NOT let a cancelled
                # child task's `CancelledError` propagate through the `await` -- it's returned as
                # a plain value in `pages`, same as any other exception. The `isinstance(page,
                # Exception)` branch above never catches it (`CancelledError` is a `BaseException`,
                # not an `Exception`, since Python 3.8), so without this branch it would fall
                # through both and be silently discarded -- not logged, not propagated, just gone.
                # Re-raise explicitly so a real cancellation/interrupt signal is never lost.
                raise page
        return fetched

    def _build_query(self, *, site: str, domain: str) -> str:
        """A single search query aimed at the privacy/DPO contact signal for this site.

        Kept generic (no per-site contacts baked in — R20): the *upstream engines* surface a site's
        actual privacy-policy contact address or data-protection-request page; this query just
        points them at that, and the LLM synthesizes from what comes back.
        """
        return (
            f"{site} {domain} privacy policy data protection officer DPO contact email "
            "GDPR data deletion request"
        )

    def _system_prompt(self) -> str:
        # Real report: for a site whose sources contained four legible departmental addresses
        # (a general contact, advertising, editorial, recruiting) but no dedicated privacy/DPO
        # one, the LLM answered UNKNOWN -- it had correctly followed a single "prefer a dedicated
        # contact, else UNKNOWN" instruction literally, but that instruction conflates "no
        # *dedicated privacy* contact" with "no usable contact at all". A human in that position
        # would obviously use the general contact address; there's no reason this optional,
        # human-confirmed *suggestion* (never applied without `case_actions.confirm_recipient()`'s
        # explicit confirmation -- R1's spirit) should be more conservative than that. Rewritten
        # as an explicit two-tier instruction so the LLM has a real fallback tier instead of only
        # "the best kind of contact, or nothing".
        return (
            "You find the contact address a data-deletion (right-to-be-forgotten) request should "
            "be sent to for a given website, based on web search results about that site's "
            "operator. Respond with ONLY a single JSON object and nothing else, of the exact "
            'shape: {"recipient": <string or null>, "reasoning": <string>}. The recipient value '
            "is a single contact email address, exactly as it appears in the sources, chosen as "
            "follows:\n"
            "1. If the sources name a dedicated privacy/data-protection contact (for example a "
            '"privacy@" or "dpo@" alias, or an address named in the privacy policy specifically '
            "for data requests), use that.\n"
            "2. Otherwise, if the sources contain any other legitimate first-party contact "
            "address for the site itself (a general contact/support/info/editorial address), use "
            "the most general one available -- prefer it over a narrowly departmental address "
            "(e.g. recruiting, advertising, sales) if a more general option exists. State in "
            "reasoning that no dedicated privacy contact was found and this is the best available "
            "general contact instead.\n"
            f'3. Only return "{_UNKNOWN_MARKER}" (or JSON null) if the sources contain no usable '
            "contact email address at all -- not merely "
            '"no dedicated privacy address" -- e.g. only a phone number or physical address, or '
            "nothing about the site's own contact details. Never guess or invent an address that "
            "doesn't appear in the sources.\n"
            "Keep reasoning to one or two sentences citing what in the results led to the answer."
        )

    def _user_prompt(
        self,
        *,
        site: str,
        domain: str,
        results: list[SearchResult],
        fetched_pages: dict[str, FetchedPage] | None = None,
    ) -> str:
        fetched_pages = fetched_pages or {}
        lines = [
            f"Site: {site}",
            f"Domain: {domain}",
            "",
            "Web search results:",
        ]
        for index, result in enumerate(results, start=1):
            snippet = result.snippet[:_MAX_SNIPPET_CHARS]
            lines.append(f"{index}. {result.title} <{result.url}>")
            if snippet:
                lines.append(f"   Snippet: {snippet}")
            fetched = fetched_pages.get(result.url)
            if fetched is not None and fetched.text:
                lines.append(f"   Full page text: {fetched.text[:_MAX_FETCHED_TEXT_CHARS]}")
        lines.append("")
        lines.append(
            "What is the best contact address to send a data-deletion request to for this site? "
            "Answer with the JSON object described above."
        )
        return "\n".join(lines)

    def _parse_response(self, content: str) -> tuple[str, str] | None:
        """Extract `(recipient, reasoning)` from the LLM's reply, or `None` if low-confidence.

        Tolerates the LLM wrapping its JSON object in surrounding prose or markdown fences by
        scanning for the first balanced-looking `{...}` span. A malformed/unparseable reply, or an
        empty/`null`/sentinel recipient, is treated as "couldn't determine" (`None`) rather than
        raised: a model that fails to follow the format is a low-confidence outcome for this
        optional feature, not a hard error -- the caller degrades to the manual prompt either way.
        """
        obj = self._extract_json_object(content)
        if obj is None:
            return None
        raw_recipient = obj.get("recipient")
        if not isinstance(raw_recipient, str):
            # Includes JSON null (the instructed "can't determine" signal) and any non-string.
            return None
        recipient = raw_recipient.strip()
        if not recipient or recipient.upper() == _UNKNOWN_MARKER:
            return None
        raw_reasoning = obj.get("reasoning")
        reasoning = raw_reasoning.strip() if isinstance(raw_reasoning, str) else ""
        return recipient, reasoning

    @staticmethod
    def _extract_json_object(content: str) -> dict[str, object] | None:
        """Return the first JSON object embedded in `content`, or `None` if none parses.

        Tries the whole string first, then falls back to the substring between the first `{` and
        the last `}` -- enough to recover a JSON object wrapped in prose or ```json fences without
        pulling in a real markdown/JSON-extraction dependency for this narrow need.
        """
        for candidate in RecipientDetector._json_candidates(content):
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _json_candidates(content: str) -> list[str]:
        candidates = [content]
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(content[start : end + 1])
        return candidates
