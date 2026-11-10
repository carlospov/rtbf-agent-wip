"""Tests for `JurisdictionDetector` (issue #63): compose `WebSearchProvider` + `LLMProvider` into
a *proposal*, never an assignment.

No real SearXNG instance or LLM vendor anywhere here -- both collaborators are fakes injected the
same way every other capability in this repo is tested (`tests/test_agent_loop_mock.py`,
`tests/plugins/senders/test_smtp.py`).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from rtbf_agent.core.interfaces import (
    FetchedPage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    SearchResult,
    WebFetchProvider,
    WebSearchProvider,
)
from rtbf_agent.plugins.jurisdiction_detection.detector import (
    JurisdictionAttempt,
    JurisdictionDetector,
    JurisdictionProposal,
)


class _FakeWebSearchProvider(WebSearchProvider):
    """Returns a fixed, injected sequence of results -- never touches a real search backend."""

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self._results = results if results is not None else []
        self.queries: list[str] = []

    async def search(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        return self._results


class _RaisingWebSearchProvider(WebSearchProvider):
    """Simulates an unreachable/misconfigured search backend."""

    async def search(self, query: str) -> list[SearchResult]:
        raise RuntimeError("SearXNG instance unreachable at http://example.invalid")


class _FakeLLMProvider(LLMProvider):
    """Returns a fixed, injected canned response -- never touches a real LLM vendor."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(content=self._content, model=request.model)


class _RaisingLLMProvider(LLMProvider):
    """Simulates an LLM transport failure."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("LLM provider unreachable")


def _result(url: str = "https://example.com/privacy") -> SearchResult:
    return SearchResult(
        title="Example Corp Privacy Policy",
        url=url,
        snippet="Example Corp is headquartered in Dublin, Ireland and processes data under GDPR.",
    )


async def test_propose_returns_jurisdiction_and_reasoning_from_llm_json_response() -> None:
    websearch = _FakeWebSearchProvider([_result()])
    llm = _FakeLLMProvider(
        json.dumps(
            {
                "jurisdiction": "GDPR",
                "reasoning": "The privacy policy states the company is based in Ireland (EU).",
            }
        )
    )
    detector = JurisdictionDetector(websearch, llm, model="mock-model")

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is not None
    assert attempt.proposal.jurisdiction == "GDPR"
    assert "Ireland" in attempt.proposal.reasoning
    assert attempt.proposal.sources == ("https://example.com/privacy",)
    # The query actually reached the search backend, mentioning the site under research, and is
    # also recorded on the attempt itself alongside the results fed to the LLM.
    assert "example.com" in websearch.queries[0]
    assert "Example" in websearch.queries[0]
    assert attempt.query == websearch.queries[0]
    assert attempt.results == (_result(),)


async def test_propose_tolerates_json_wrapped_in_surrounding_prose() -> None:
    websearch = _FakeWebSearchProvider([_result()])
    llm = _FakeLLMProvider(
        'Sure, here is my answer:\n```json\n{"jurisdiction": "CCPA", "reasoning": "California."}'
        "\n```\nLet me know if you need anything else."
    )
    detector = JurisdictionDetector(websearch, llm, model="mock-model")

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is not None
    assert attempt.proposal.jurisdiction == "CCPA"


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"jurisdiction": None, "reasoning": "not enough evidence"}),
        json.dumps({"jurisdiction": "UNKNOWN", "reasoning": "not enough evidence"}),
        json.dumps({"jurisdiction": "  ", "reasoning": "not enough evidence"}),
        "not even json",
        json.dumps({"no_jurisdiction_key": "oops"}),
    ],
)
async def test_propose_returns_none_when_llm_is_not_confident_or_malformed(content: str) -> None:
    websearch = _FakeWebSearchProvider([_result()])
    llm = _FakeLLMProvider(content)
    detector = JurisdictionDetector(websearch, llm, model="mock-model")

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is None
    # The query/results are still captured on the attempt even though no proposal was reached --
    # this is the audit evidence for a "couldn't determine" outcome (real report).
    assert attempt.query == websearch.queries[0]
    assert attempt.results == (_result(),)


async def test_propose_returns_none_when_search_finds_nothing() -> None:
    websearch = _FakeWebSearchProvider([])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model")

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is None
    assert attempt.results == ()
    assert attempt.query == websearch.queries[0]
    # The LLM must never even be called if there's nothing to synthesize from (R19: minimum data).
    assert llm.requests == []


async def test_propose_propagates_a_websearch_failure_rather_than_swallowing_it() -> None:
    """`JurisdictionDetector.propose()` itself does not degrade on a collaborator failure -- that
    policy lives one layer up, in `cli._propose_jurisdiction()`, which is what actually decides an
    optional enhancement's failure must never crash a run. This module stays honest about real
    failures instead of silently hiding them from every caller."""
    detector = JurisdictionDetector(_RaisingWebSearchProvider(), _FakeLLMProvider("{}"), model="m")

    with pytest.raises(RuntimeError, match="unreachable"):
        await detector.propose(site="Example", domain="example.com")


async def test_propose_propagates_an_llm_failure_rather_than_swallowing_it() -> None:
    detector = JurisdictionDetector(
        _FakeWebSearchProvider([_result()]), _RaisingLLMProvider(), model="m"
    )

    with pytest.raises(RuntimeError, match="unreachable"):
        await detector.propose(site="Example", domain="example.com")


async def test_propose_caps_results_fed_to_the_llm_at_max_results() -> None:
    many_results = [_result(url=f"https://example.com/{i}") for i in range(20)]
    websearch = _FakeWebSearchProvider(many_results)
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model", max_results=3)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is not None
    assert len(attempt.proposal.sources) == 3
    assert len(attempt.results) == 3


def test_jurisdiction_proposal_is_frozen_and_sources_is_a_tuple() -> None:
    proposal = JurisdictionProposal(jurisdiction="GDPR", reasoning="x", sources=("a", "b"))
    with pytest.raises(AttributeError):
        proposal.jurisdiction = "CCPA"  # type: ignore[misc]
    assert isinstance(proposal.sources, tuple)


def test_jurisdiction_attempt_is_frozen_and_results_is_a_tuple() -> None:
    attempt = JurisdictionAttempt(query="q", results=(_result(),), proposal=None)
    with pytest.raises(AttributeError):
        attempt.query = "other"  # type: ignore[misc]
    assert isinstance(attempt.results, tuple)


def test_jurisdiction_attempt_fetched_urls_defaults_to_empty_tuple() -> None:
    """Every pre-existing call site constructs a `JurisdictionAttempt` without `fetched_urls` --
    it must default to `()`, not require every existing construction to be updated."""
    attempt = JurisdictionAttempt(query="q", results=(), proposal=None)
    assert attempt.fetched_urls == ()


# --- optional page-fetch enrichment (real report: a search snippet can miss info -- e.g. a
# contact email -- that only lives in the page body) ------------------------------------------


class _FakeWebFetchProvider(WebFetchProvider):
    """Returns a fixed, injected page per URL (or `None`/raises), never touches a real backend."""

    def __init__(
        self,
        pages: dict[str, FetchedPage] | None = None,
        *,
        raise_for: set[str] | None = None,
    ) -> None:
        self._pages = pages or {}
        self._raise_for = raise_for or set()
        self.fetched_urls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage | None:
        self.fetched_urls.append(url)
        if url in self._raise_for:
            raise RuntimeError(f"could not fetch {url}")
        return self._pages.get(url)


async def test_propose_without_webfetch_reproduces_snippet_only_behavior() -> None:
    """The default (`webfetch=None`) must reproduce this class's pre-existing prompt exactly --
    additive-only, no behavior change for every caller that doesn't opt in."""
    websearch = _FakeWebSearchProvider([_result()])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model")

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is not None
    assert attempt.fetched_urls == ()
    prompt = llm.requests[0].messages[-1].content
    assert "Full page text" not in prompt


async def test_propose_with_webfetch_includes_fetched_page_text_in_the_prompt() -> None:
    # A URL that deliberately doesn't match any of crawl_site()'s own default common paths/link
    # keywords (see test_crawl.py) -- crawl_site() also being tried first (and finding nothing,
    # since this fake has no page for the homepage or any common path) is exercised by its own
    # dedicated tests below; this test is about the *search-result* fetch-enrichment path.
    url = "https://example.com/vendor-info-page"
    webfetch = _FakeWebFetchProvider({url: FetchedPage(url=url, text="the real contact info")})
    websearch = _FakeWebSearchProvider([_result(url=url)])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model", webfetch=webfetch)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert url in webfetch.fetched_urls
    assert attempt.fetched_urls == (url,)
    prompt = llm.requests[0].messages[-1].content
    assert "the real contact info" in prompt


async def test_propose_caps_fetched_page_text_in_the_prompt() -> None:
    """code-reviewer finding: the prompt must cap fetched page text itself, not just trust
    `WebFetchProvider`'s own contract (which only promises text is "already capped by the
    adapter", not to what limit, nor that every adapter honors it) -- mirrors `_MAX_SNIPPET_CHARS`
    already applied to search snippets."""
    from rtbf_agent.plugins.jurisdiction_detection.detector import _MAX_FETCHED_TEXT_CHARS

    url = "https://example.com/vendor-info-page"  # avoid colliding with crawl_site()'s own paths
    oversized_text = "x" * (_MAX_FETCHED_TEXT_CHARS + 500)
    webfetch = _FakeWebFetchProvider({url: FetchedPage(url=url, text=oversized_text)})
    websearch = _FakeWebSearchProvider([_result(url=url)])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model", webfetch=webfetch)

    await detector.propose(site="Example", domain="example.com")

    prompt = llm.requests[0].messages[-1].content
    assert oversized_text not in prompt
    assert "x" * _MAX_FETCHED_TEXT_CHARS in prompt


async def test_propose_only_fetches_top_n_results() -> None:
    results = [_result(url=f"https://example.com/{i}") for i in range(5)]
    webfetch = _FakeWebFetchProvider({})
    websearch = _FakeWebSearchProvider(results)
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(
        websearch, llm, model="mock-model", webfetch=webfetch, fetch_top_n=2
    )

    await detector.propose(site="Example", domain="example.com")

    # The last two fetches are the search-result enrichment this test is about; crawl_site() is
    # tried (and, with an empty _pages dict, fails to find anything) first, so it also shows up
    # earlier in fetched_urls -- crawl_site() fully completes before the search+fetch_top_n path
    # ever starts, so this ordering is structural, not a timing coincidence.
    assert webfetch.fetched_urls[-2:] == ["https://example.com/0", "https://example.com/1"]


async def test_propose_survives_a_single_url_fetch_failure() -> None:
    """One URL failing to fetch must not abort the whole attempt -- it just falls back to that
    result's snippet alone (R9's "optional enhancement degrades gracefully" spirit)."""
    ok_url = "https://example.com/ok"
    bad_url = "https://example.com/bad"
    webfetch = _FakeWebFetchProvider(
        {ok_url: FetchedPage(url=ok_url, text="fetched text")}, raise_for={bad_url}
    )
    websearch = _FakeWebSearchProvider([_result(url=bad_url), _result(url=ok_url)])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model", webfetch=webfetch)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is not None
    assert attempt.fetched_urls == (ok_url,)
    prompt = llm.requests[0].messages[-1].content
    assert "fetched text" in prompt


async def test_propose_does_not_persist_fetched_text_on_the_attempt_itself() -> None:
    """`Attempt.fetched_urls` records *which* URLs were fetched, never the fetched text -- a full
    page is a much larger, unbounded blob that has no business in an audit-log Event.detail."""
    url = "https://example.com/vendor-info-page"  # avoid colliding with crawl_site()'s own paths
    webfetch = _FakeWebFetchProvider({url: FetchedPage(url=url, text="sensitive fetched content")})
    websearch = _FakeWebSearchProvider([_result(url=url)])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model", webfetch=webfetch)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.fetched_urls == (url,)
    assert not any("sensitive fetched content" in str(r) for r in attempt.results)


async def test_propose_reraises_a_cancelled_fetch_rather_than_silently_discarding_it() -> None:
    """code-reviewer finding: `asyncio.gather(return_exceptions=True)` returns a cancelled child
    task's `CancelledError` as a plain value, not as something that propagates through the
    `await` -- so `isinstance(page, Exception)` alone (CancelledError is a BaseException, not an
    Exception) would silently discard it. It must be re-raised, not treated as routine "optional
    enhancement" noise."""

    class _CancellingWebFetchProvider(WebFetchProvider):
        async def fetch(self, url: str) -> FetchedPage | None:
            raise asyncio.CancelledError("cancelled")

    websearch = _FakeWebSearchProvider([_result()])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(
        websearch, llm, model="mock-model", webfetch=_CancellingWebFetchProvider()
    )

    with pytest.raises(asyncio.CancelledError):
        await detector.propose(site="Example", domain="example.com")


# --- crawl-first, web-search-fallback (real report: the user wants the agent to navigate the
# site it was given a URL for, not run a generic web search) ------------------------------------


async def test_propose_tries_crawling_the_site_before_web_search() -> None:
    """When crawling the site itself finds usable pages, `websearch.search()` must never be called
    at all -- crawling is the primary path, not merely tried first on every attempt."""
    homepage = FetchedPage(
        url="https://example.com/", text="welcome", links=("https://example.com/privacy",)
    )
    privacy_page = FetchedPage(url="https://example.com/privacy", text="Governed by Irish law.")
    webfetch = _FakeWebFetchProvider(
        {"https://example.com/": homepage, "https://example.com/privacy": privacy_page}
    )
    websearch = _FakeWebSearchProvider([_result()])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "Irish law -> EU"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model", webfetch=webfetch)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert websearch.queries == []  # never called
    assert attempt.proposal is not None
    assert attempt.proposal.jurisdiction == "GDPR"
    assert attempt.query == "crawl https://example.com/"
    assert set(attempt.fetched_urls) == {"https://example.com/", "https://example.com/privacy"}
    prompt = llm.requests[0].messages[-1].content
    assert "Governed by Irish law." in prompt


async def test_propose_falls_through_to_web_search_when_crawling_finds_nothing() -> None:
    """The opposite case: an empty crawl (webfetch configured but finds no usable pages at all)
    must fall through to the pre-existing keyword-search flow, unchanged."""
    webfetch = _FakeWebFetchProvider({})  # every fetch() call returns None
    websearch = _FakeWebSearchProvider([_result()])
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(websearch, llm, model="mock-model", webfetch=webfetch)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert websearch.queries != []  # search *was* used, as the fallback
    assert attempt.proposal is not None
    assert attempt.query == websearch.queries[0]


async def test_propose_works_with_no_search_backend_configured_at_all() -> None:
    """`websearch=None` (e.g. no SearXNG configured) must not prevent detection when crawling
    alone finds something -- a crawl-only detector is a fully-working one."""
    homepage = FetchedPage(url="https://example.com/", text="Governed by Irish law.", links=())
    webfetch = _FakeWebFetchProvider({"https://example.com/": homepage})
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(None, llm, model="mock-model", webfetch=webfetch)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is not None
    assert attempt.proposal.jurisdiction == "GDPR"


async def test_propose_returns_none_proposal_when_neither_crawl_nor_search_available() -> None:
    """No `webfetch`, no `websearch` -- must degrade to "couldn't determine" rather than raising
    (e.g. an `AttributeError` on a `None` websearch)."""
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(None, llm, model="mock-model")

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is None
    assert attempt.query == ""
    assert attempt.results == ()
    assert llm.requests == []  # never even called


async def test_propose_returns_none_proposal_when_websearch_none_and_crawl_finds_nothing() -> None:
    webfetch = _FakeWebFetchProvider({})
    llm = _FakeLLMProvider(json.dumps({"jurisdiction": "GDPR", "reasoning": "n/a"}))
    detector = JurisdictionDetector(None, llm, model="mock-model", webfetch=webfetch)

    attempt = await detector.propose(site="Example", domain="example.com")

    assert attempt.proposal is None
    assert llm.requests == []
