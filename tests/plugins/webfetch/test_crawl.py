"""Tests for `rtbf_agent.plugins.webfetch.crawl.crawl_site()`.

No real HTTP/DNS anywhere here -- a fake `WebFetchProvider` stands in, same fake-collaborator
pattern this repo already uses everywhere else.
"""

from __future__ import annotations

from rtbf_agent.core.interfaces import FetchedPage, WebFetchProvider
from rtbf_agent.plugins.webfetch.crawl import crawl_site


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


async def test_crawl_site_fetches_the_homepage_first() -> None:
    homepage = FetchedPage(url="https://example.com/", text="welcome", links=())
    webfetch = _FakeWebFetchProvider({"https://example.com/": homepage})

    pages = await crawl_site(webfetch, "https://example.com/")

    assert pages == [homepage]
    assert webfetch.fetched_urls[0] == "https://example.com/"


async def test_crawl_site_returns_empty_when_homepage_is_unreachable() -> None:
    webfetch = _FakeWebFetchProvider({}, raise_for={"https://example.com/"})

    pages = await crawl_site(webfetch, "https://example.com/")

    assert pages == []


async def test_crawl_site_follows_same_domain_keyword_matching_links() -> None:
    privacy_page = FetchedPage(url="https://example.com/privacy", text="privacy info")
    homepage = FetchedPage(
        url="https://example.com/",
        text="welcome",
        links=(
            "https://example.com/privacy",
            "https://example.com/products",  # no keyword match -- must be skipped
            "https://other-site.invalid/contact",  # off-domain -- must be skipped
        ),
    )
    webfetch = _FakeWebFetchProvider(
        {"https://example.com/": homepage, "https://example.com/privacy": privacy_page}
    )

    pages = await crawl_site(webfetch, "https://example.com/", common_paths=())

    assert homepage in pages
    assert privacy_page in pages
    assert "https://example.com/products" not in webfetch.fetched_urls
    assert "https://other-site.invalid/contact" not in webfetch.fetched_urls


async def test_crawl_site_tries_common_paths() -> None:
    contact_page = FetchedPage(url="https://example.com/contact", text="contact us here")
    homepage = FetchedPage(url="https://example.com/", text="welcome", links=())
    webfetch = _FakeWebFetchProvider(
        {"https://example.com/": homepage, "https://example.com/contact": contact_page}
    )

    pages = await crawl_site(
        webfetch, "https://example.com/", common_paths=("/contact",), link_keywords=()
    )

    assert contact_page in pages


async def test_crawl_site_deduplicates_a_common_path_already_seen_as_a_link() -> None:
    contact_page = FetchedPage(url="https://example.com/contact", text="contact us here")
    homepage = FetchedPage(
        url="https://example.com/", text="welcome", links=("https://example.com/contact",)
    )
    webfetch = _FakeWebFetchProvider(
        {"https://example.com/": homepage, "https://example.com/contact": contact_page}
    )

    await crawl_site(
        webfetch, "https://example.com/", common_paths=("/contact",), link_keywords=("contact",)
    )

    assert webfetch.fetched_urls.count("https://example.com/contact") == 1


async def test_crawl_site_never_refetches_the_homepage_via_a_common_path() -> None:
    homepage = FetchedPage(url="https://example.com/", text="welcome", links=())
    webfetch = _FakeWebFetchProvider({"https://example.com/": homepage})

    await crawl_site(webfetch, "https://example.com/", common_paths=("/",), link_keywords=())

    assert webfetch.fetched_urls.count("https://example.com/") == 1


async def test_crawl_site_never_refetches_the_homepage_when_base_url_has_no_trailing_slash() -> (
    None
):
    """code-reviewer finding: `urljoin(base_url, "/")` resolves to `"https://example.com/"` even
    when `base_url` itself is `"https://example.com"` (no trailing slash) -- without normalizing
    both forms into `seen`, the "/" common path would resolve to a URL that looks different from
    `base_url` and get fetched a second time, wasting a fetch slot."""
    homepage = FetchedPage(url="https://example.com", text="welcome", links=())
    webfetch = _FakeWebFetchProvider({"https://example.com": homepage})

    await crawl_site(webfetch, "https://example.com", common_paths=("/",), link_keywords=())

    assert len(webfetch.fetched_urls) == 1


async def test_crawl_site_caps_total_fetches_at_max_pages() -> None:
    homepage = FetchedPage(url="https://example.com/", text="welcome", links=())
    webfetch = _FakeWebFetchProvider({"https://example.com/": homepage})
    many_paths = tuple(f"/page{i}" for i in range(20))

    await crawl_site(webfetch, "https://example.com/", common_paths=many_paths, max_pages=3)

    assert len(webfetch.fetched_urls) == 3  # homepage + 2 candidates


async def test_crawl_site_shares_one_budget_between_links_and_common_paths() -> None:
    """code-reviewer finding: link-based candidates and common-path candidates must share a single
    `max_pages - 1` budget, not each get their own (which would silently double the effective
    limit) -- a homepage with several keyword-matching links *and* a long common-paths list, capped
    tightly, must still only ever fetch `max_pages` pages total."""
    homepage = FetchedPage(
        url="https://example.com/",
        text="welcome",
        links=(
            "https://example.com/privacy-link-1",
            "https://example.com/contact-link-2",
            "https://example.com/legal-link-3",
        ),
    )
    webfetch = _FakeWebFetchProvider({"https://example.com/": homepage})
    many_paths = tuple(f"/page{i}" for i in range(20))

    await crawl_site(
        webfetch,
        "https://example.com/",
        common_paths=many_paths,
        link_keywords=("privacy", "contact", "legal"),
        max_pages=4,
    )

    assert len(webfetch.fetched_urls) == 4  # homepage + 3 candidates total, not 1 + 3 + 20


async def test_crawl_site_survives_a_single_candidate_fetch_failure() -> None:
    homepage = FetchedPage(url="https://example.com/", text="welcome", links=())
    contact_page = FetchedPage(url="https://example.com/contact", text="contact us")
    webfetch = _FakeWebFetchProvider(
        {"https://example.com/": homepage, "https://example.com/contact": contact_page},
        raise_for={"https://example.com/privacy"},
    )

    pages = await crawl_site(
        webfetch,
        "https://example.com/",
        common_paths=("/privacy", "/contact"),
        link_keywords=(),
    )

    assert contact_page in pages
    assert all(p.url != "https://example.com/privacy" for p in pages)
