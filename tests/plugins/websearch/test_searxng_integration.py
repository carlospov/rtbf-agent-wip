"""Real-network integration test for `SearXNGProvider` (issue #65 follow-up).

`tests/plugins/websearch/test_searxng.py` proves the wire-format translation logic against
`httpx.MockTransport` -- fast, deterministic, no external dependency, and what runs by default.
This module is the deliberate exception: one real query against a real, running SearXNG instance,
which in turn queries real upstream search engines/real websites, to prove the assumptions the
mock encodes (JSON shape, `results[].title/url/content` keys) actually hold against a live server.

Skipped unless `RTBF_SEARXNG_BASE_URL` is set, so it never runs by accident on a machine without a
SearXNG instance available -- CI provides one via `.github/workflows/ci.yml` (a throwaway instance
built from `.github/searxng/settings.yml`, `search.formats: [html, json]` enabled). To run this
locally:

    docker run -d --name searxng-local -p 8080:8080 \\
        -v "$PWD/.github/searxng/settings.yml:/etc/searxng/settings.yml:ro" \\
        searxng/searxng:2026.7.14-58e02a01a
    RTBF_SEARXNG_BASE_URL=http://localhost:8080 uv run pytest -m integration

Deliberately asserts only on shape (non-empty results, well-formed fields), never on which specific
sites rank for a query -- real search results/rankings drift over time and across instances; a
test asserting exact content would be flaky by construction, not a bug in the adapter.
"""

from __future__ import annotations

import os

import pytest

from rtbf_agent.core.interfaces import SearchResult
from rtbf_agent.plugins.websearch.config import SearXNGConfig
from rtbf_agent.plugins.websearch.searxng import SearXNGProvider

pytestmark = pytest.mark.integration

requires_live_searxng = pytest.mark.skipif(
    not os.environ.get("RTBF_SEARXNG_BASE_URL"),
    reason="set RTBF_SEARXNG_BASE_URL to a running SearXNG instance to run this test",
)


@requires_live_searxng
async def test_search_against_a_real_searxng_instance_returns_well_formed_results() -> None:
    provider = SearXNGProvider(SearXNGConfig(base_url=os.environ["RTBF_SEARXNG_BASE_URL"]))

    # A stable, high-traffic query, not anything RTBF-Agent-specific: the goal is proving the
    # wire format holds against a real server, not testing search quality/rankings.
    results = await provider.search("Wikipedia")

    assert results, "expected at least one real result for a high-traffic query"
    for result in results:
        assert isinstance(result, SearchResult)
        assert result.title
        assert result.url.startswith("http")
        # `snippet` is allowed to be empty (some upstream engines return no description), unlike
        # title/url which every real search backend supplies.
