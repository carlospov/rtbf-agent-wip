"""`JurisdictionDetector` — compose web search + LLM into a *proposed* jurisdiction (issue #63).

This is the first genuinely decision-driving use of the LLM in this project. Unlike
`agent_loop._classify()` — whose LLM response is stored purely as an audit summary and never
influences `Case.applicable_jurisdiction`/`drafting_language`/`state` — this module's output
becomes a *proposed value* a human then acts on. Two hard boundaries make that safe (R1's spirit,
extended to jurisdiction selection even though it isn't itself an "irreversible action"):

1. **This module never writes to a `Case`.** `propose()` returns a plain `JurisdictionAttempt`
   value object (or raises); it does not touch the database, mutate a `Case`, or record an
   `Event`. Turning a proposal into a persisted `Case.applicable_jurisdiction` happens only in
   `cli.prompt_case_overrides()`, and only *after* the human has seen it as a suggested default
   and either accepted (blank input) or overridden (typed a different value) it. There is no code
   path from a proposal to a stored jurisdiction that skips that prompt.
2. **This module is a plugin, not core.** The "search the web, ask the LLM to synthesize a
   jurisdiction code" logic lives here, behind the already-injected `WebSearchProvider` +
   `LLMProvider` contracts (R16, R20) — never inline in `core/`, `agent_loop.py`, or `cli.py`.
   Adding a different search/LLM backend is a plugin swap, not a change here (R21).

`propose()` distinguishes two non-crash outcomes, and its caller (`cli._propose_jurisdiction()`)
treats both as "no proposal" for the purpose of the manual prompt (issue #43's unchanged behavior),
but records different audit `Event`s for each so the case history can tell them apart:

- **`JurisdictionAttempt(proposal=None)` — low confidence.** Search returned nothing usable, or the
  LLM couldn't name a jurisdiction it was confident about (an empty/`null`/`"UNKNOWN"` answer, or an
  unparseable one). This is an expected, non-error outcome and does *not* raise — `.query`/
  `.results` are still populated so the caller can log what was searched.
- **an exception — the capability failed.** Web search was unreachable/misconfigured, or the LLM
  transport errored. `propose()` lets those propagate; `cli`'s orchestration layer is where the
  "an optional enhancement failing must never crash the run" policy is applied (see
  `cli._propose_jurisdiction()`), so this module stays honest about real failures rather than
  swallowing them and silently degrading with no signal.

Deliberately jurisdiction-agnostic: the LLM may propose *any* code (`"GDPR"`, `"CCPA"`, a code for
a jurisdiction this repo has no legal template for yet). No jurisdiction list is hardcoded here —
that would re-introduce exactly the per-jurisdiction knowledge R20 keeps out of shared code. The
human confirmation step, and later `LegalTemplateProvider.get_template()`, are what reconcile a
proposed code against what the project can actually act on.

**Crawl the site first, web search only as a fallback.** Real report: a user adding a case already
gives the agent the site's own URL -- they want the agent to navigate *that site* looking for its
own privacy/legal page, not run a keyword search across the whole web and hope a relevant result
surfaces (the `3djuegos.com` bug that motivated `plugins/webfetch/` in the first place was itself a
symptom of the search-first approach: mostly irrelevant results from unrelated companies). When a
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
"""The sentinel the LLM is instructed to return when it cannot confidently determine a
jurisdiction — mapped to a `None` proposal (low confidence), never surfaced as a real code."""


def _cap_fetched_page_text(page: FetchedPage) -> FetchedPage:
    """Re-cap `page.text` at `_MAX_FETCHED_TEXT_CHARS` regardless of the source (a search-result
    fetch or a crawled page) -- shared by `_fetch_top_results()` and `_propose_from_crawl()` so
    the "don't trust the adapter's own cap as a contract guarantee" rule (see
    `_MAX_FETCHED_TEXT_CHARS`'s docstring) applies identically to both."""
    return FetchedPage(url=page.url, text=page.text[:_MAX_FETCHED_TEXT_CHARS])


@dataclass(frozen=True)
class JurisdictionProposal:
    """A *suggested* jurisdiction code plus the reasoning/sources behind it — never an assignment.

    Holds no `Case` reference and is never itself persisted: `cli` records the reasoning/sources as
    a `jurisdiction_proposed` `Event` (audit trail, same spirit as `classification_proposed`) and
    surfaces `jurisdiction` to the human as a suggested prompt default. Only the human's response
    to that prompt ever reaches `Case.applicable_jurisdiction`.
    """

    jurisdiction: str
    """The proposed jurisdiction code (e.g. `"GDPR"`). Always non-empty and not the low-confidence
    sentinel — `propose()` returns a `JurisdictionAttempt` with `proposal=None` in those cases."""
    reasoning: str
    """The LLM's short explanation of *why* it proposed this code, recorded for audit so a human
    can judge the suggestion rather than take it on faith."""
    sources: tuple[str, ...]
    """The result URLs the proposal was synthesized from — the evidence trail behind the reasoning.
    A `tuple` (not a `list`) so a frozen `JurisdictionProposal` can't have its sources mutated in
    place after construction."""


@dataclass(frozen=True)
class JurisdictionAttempt:
    """Everything `propose()` learned in one call, whether or not it reached a confident proposal.

    `propose()` always returns one of these (never a bare `None`) so a caller that wants to log
    "what was searched and what came back" (real report: an operator with a silently-degrading
    search/LLM stack had no way to tell that apart from detection never having run at all) has the
    evidence available even when `.proposal` ends up `None` -- the query and search results are the
    audit trail for a "couldn't determine" outcome, same as `.reasoning`/`.sources` are for a
    successful one. Only the *exception* path (websearch/LLM transport failure) skips this --
    `propose()` still raises for that, since there is no query/results to report when the search
    itself never completed usefully.
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
    proposal: JurisdictionProposal | None
    """The confident proposal reached from `results`, or `None` if search returned nothing usable
    or the LLM wasn't confident enough to name a jurisdiction."""
    fetched_urls: tuple[str, ...] = ()
    """Which of `results`' URLs were also successfully fetched for full page content (only
    non-empty when a `webfetch` collaborator was configured) -- an audit signal that the LLM saw
    more than just a search snippet for these, not the fetched text itself: a full page is a much
    larger, unbounded blob that has no business living in an audit-log `Event.detail` JSON column
    (the same lesson `cli._search_results_detail()`'s snippet cap already applies to `results`)."""


class JurisdictionDetector:
    """Composes an injected `WebSearchProvider` + `LLMProvider` into a jurisdiction *proposal*.

    Both collaborators are injected (not constructed here), mirroring every other capability in
    this repo (`agent_loop`, `cli.run_once`) — so this class can be exercised in tests against a
    fake search backend and a fake LLM without a real SearXNG instance or LLM vendor, and so
    swapping either backend is a wiring change at the call site, never a change here (R21).
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

    async def propose(self, *, site: str, domain: str) -> JurisdictionAttempt:
        """Research `domain` and return the full attempt, `.proposal` set if confident.

        Tries crawling the site itself first (if `webfetch` is configured); only falls through to
        the keyword-search flow below if that finds no usable pages at all -- see the class
        docstring's "crawl first" section. `.proposal` is `None` (an expected, non-error outcome)
        when neither approach turns up anything usable, or the LLM can't confidently name a
        jurisdiction from what was found -- `.query`/`.results` are still populated so the caller
        can log what was tried. Raises (does not swallow) if web search or the LLM *fails* -- the
        caller decides how to degrade (see the module docstring); there is no `JurisdictionAttempt`
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
            return JurisdictionAttempt(query="", results=(), proposal=None)
        results = list(await self._websearch.search(query))
        if not results:
            # Nothing to synthesize from -- a legitimate "can't determine", not a failure. The
            # caller falls through to the manual prompt exactly as if detection were disabled.
            return JurisdictionAttempt(query=query, results=(), proposal=None)

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
            return JurisdictionAttempt(
                query=query, results=tuple(results), proposal=None, fetched_urls=fetched_urls
            )
        jurisdiction, reasoning = proposed
        proposal = JurisdictionProposal(
            jurisdiction=jurisdiction,
            reasoning=reasoning,
            sources=tuple(r.url for r in results if r.url),
        )
        return JurisdictionAttempt(
            query=query, results=tuple(results), proposal=proposal, fetched_urls=fetched_urls
        )

    async def _propose_from_crawl(self, *, site: str, domain: str) -> JurisdictionAttempt | None:
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
            return JurisdictionAttempt(
                query=query, results=results, proposal=None, fetched_urls=fetched_urls
            )
        jurisdiction, reasoning = proposed
        proposal = JurisdictionProposal(
            jurisdiction=jurisdiction,
            reasoning=reasoning,
            sources=tuple(page.url for page in capped_pages),
        )
        return JurisdictionAttempt(
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
        """A single search query aimed at the legal/jurisdiction signal for this site.

        Kept generic (no per-jurisdiction terms baked in — R20): the *upstream engines* surface a
        site's actual privacy-policy governing-law clause or corporate location; this query just
        points them at that, and the LLM synthesizes from what comes back.
        """
        return f"{site} {domain} privacy policy governing law jurisdiction company headquarters"

    def _system_prompt(self) -> str:
        return (
            "You determine which data-protection jurisdiction most likely governs deletion "
            "requests to a website, based on web search results about that site's operator. "
            "Respond with ONLY a single JSON object and nothing else, of the exact shape: "
            '{"jurisdiction": <string or null>, "reasoning": <string>}. '
            "The jurisdiction value is a short code or name for the applicable data-protection "
            'regime (for example "GDPR" for the EU/EEA/UK, "CCPA" for California, "LGPD" for '
            "Brazil, or another established code where the evidence points elsewhere). "
            f'Return "{_UNKNOWN_MARKER}" (or JSON null) for the jurisdiction if the results do '
            "not let you determine it with reasonable confidence -- do not guess. "
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
            "Which data-protection jurisdiction most likely governs a deletion request to this "
            "site? Answer with the JSON object described above."
        )
        return "\n".join(lines)

    def _parse_response(self, content: str) -> tuple[str, str] | None:
        """Extract `(jurisdiction, reasoning)` from the LLM's reply, or `None` if low-confidence.

        Tolerates the LLM wrapping its JSON object in surrounding prose or markdown fences by
        scanning for the first balanced-looking `{...}` span. A malformed/unparseable reply, or an
        empty/`null`/sentinel jurisdiction, is treated as "couldn't determine" (`None`) rather than
        raised: a model that fails to follow the format is a low-confidence outcome for this
        optional feature, not a hard error -- the caller degrades to the manual prompt either way.
        """
        obj = self._extract_json_object(content)
        if obj is None:
            return None
        raw_jurisdiction = obj.get("jurisdiction")
        if not isinstance(raw_jurisdiction, str):
            # Includes JSON null (the instructed "can't determine" signal) and any non-string.
            return None
        jurisdiction = raw_jurisdiction.strip()
        if not jurisdiction or jurisdiction.upper() == _UNKNOWN_MARKER:
            return None
        raw_reasoning = obj.get("reasoning")
        reasoning = raw_reasoning.strip() if isinstance(raw_reasoning, str) else ""
        return jurisdiction, reasoning

    @staticmethod
    def _extract_json_object(content: str) -> dict[str, object] | None:
        """Return the first JSON object embedded in `content`, or `None` if none parses.

        Tries the whole string first, then falls back to the substring between the first `{` and
        the last `}` -- enough to recover a JSON object wrapped in prose or ```json fences without
        pulling in a real markdown/JSON-extraction dependency for this narrow need.
        """
        for candidate in JurisdictionDetector._json_candidates(content):
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
