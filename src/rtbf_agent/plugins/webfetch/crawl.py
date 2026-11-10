"""`crawl_site()` — navigate a site directly, rather than searching the web for it.

Real report: a user adding a case already gives the agent the site's own URL (`add <url>`) and
expects the agent to go look at *that site* for its contact/privacy info -- not run a generic
keyword search across the whole web and hope a relevant result surfaces. The `3djuegos.com` bug
that motivated `plugins/webfetch/http.py`'s existence in the first place was itself a symptom of
this: a keyword search returned mostly irrelevant pages from unrelated companies, and the one
genuinely on-site result only got considered because it happened to rank in a generic search, not
because anything went looking for it deliberately.

This module is deliberately small: fetch the homepage, follow a handful of its own same-domain
links that look relevant (by URL keyword, not full-text relevance scoring -- no search-engine-like
ranking here), and also try a short list of common paths every site-with-a-privacy-page tends to
use. Not a recursive crawler -- one hop out from the homepage, capped at `max_pages` fetches total.
`jurisdiction_detection`/`recipient_detection` both use this as their *first* attempt, falling back
to the existing `WebSearchProvider`-based keyword search only if crawling the site itself turns up
nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from urllib.parse import urljoin, urlsplit

from rtbf_agent.core.interfaces import FetchedPage, WebFetchProvider

_logger = logging.getLogger(__name__)

DEFAULT_COMMON_PATHS: tuple[str, ...] = (
    "/",
    "/contact",
    "/contacto",
    "/privacy",
    "/privacy-policy",
    "/legal",
    "/aviso-legal",
    "/terms",
    "/impressum",
    "/data-protection",
)
"""Paths worth trying on any site, regardless of what (if anything) its homepage links to --
a plugin-level heuristic list, not per-site logic (R20): the same category as the English-only
keyword phrases `jurisdiction_detection`/`recipient_detection`'s own search queries already use. A
handful of Spanish equivalents are included (`/contacto`, `/aviso-legal`) because the bug that
motivated this whole feature was a Spanish-language site."""

DEFAULT_LINK_KEYWORDS: tuple[str, ...] = (
    "privacy",
    "contact",
    "contacto",
    "legal",
    "terms",
    "aviso",
    "datos",
    "dpo",
    "impressum",
    "proteccion",
)
"""Substrings (case-insensitive) that make a homepage's own link worth following -- e.g. a footer
link to `/company/legal-notice` or `/es/aviso-legal`. Matched against the link's full URL, not
anchor text (`FetchedPage.links` carries only resolved `href` targets, not the text between the
`<a>` tags -- `plugins/webfetch/http.py` doesn't extract that, since nothing needed it before)."""

DEFAULT_MAX_PAGES = 6
"""Total fetches per `crawl_site()` call, homepage included -- bounds latency/load the same way
`DEFAULT_FETCH_TOP_N` already bounds the search-result-fetch path in both detectors."""


async def crawl_site(
    webfetch: WebFetchProvider,
    base_url: str,
    *,
    common_paths: Sequence[str] = DEFAULT_COMMON_PATHS,
    link_keywords: Sequence[str] = DEFAULT_LINK_KEYWORDS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[FetchedPage]:
    """Fetch `base_url` (the homepage) plus a few likely-relevant pages one hop out from it.

    Returns every page that was actually fetched (homepage included, if it succeeded) -- an empty
    list means the homepage itself was unreachable/unusable and nothing else was worth trying. A
    single candidate page failing to fetch is never fatal to the whole crawl (R9's "optional
    enhancement degrades gracefully" spirit, same as `_fetch_top_results()` in both detectors) --
    it's just omitted, logged at DEBUG.

    Candidate pages beyond the homepage are, in order: the homepage's own same-domain links whose
    URL matches one of `link_keywords`, then `common_paths` joined onto `base_url` -- deduplicated
    against each other and against `base_url` itself, capped at `max_pages - 1` total (the homepage
    fetch is the `+1`).
    """
    homepage = await _fetch_or_none(webfetch, base_url)
    pages: list[FetchedPage] = [homepage] if homepage is not None else []

    base_host = urlsplit(base_url).hostname
    # code-reviewer finding: `urljoin(base_url, "/")` (what the "/" common path resolves to) can
    # differ from `base_url` itself when the caller passes one without a trailing slash (e.g.
    # "https://example.com" vs "https://example.com/") -- seeding `seen` with both forms means the
    # homepage never gets fetched a second time via that common path regardless of which form the
    # caller used. Both of this module's current callers always pass a trailing slash, but this
    # function accepts `base_url` as a plain argument, not a guaranteed-normalized one.
    seen = {base_url, urljoin(base_url, "/")}
    candidates: list[str] = []

    if homepage is not None:
        for link in homepage.links:
            if len(candidates) >= max_pages - 1:
                break
            if link in seen or urlsplit(link).hostname != base_host:
                continue
            if not any(keyword in link.lower() for keyword in link_keywords):
                continue
            seen.add(link)
            candidates.append(link)

    for path in common_paths:
        if len(candidates) >= max_pages - 1:
            break
        url = urljoin(base_url, path)
        if url in seen:
            continue
        seen.add(url)
        candidates.append(url)

    if candidates:
        fetched = await asyncio.gather(*(_fetch_or_none(webfetch, url) for url in candidates))
        pages.extend(page for page in fetched if page is not None)

    return pages


async def _fetch_or_none(webfetch: WebFetchProvider, url: str) -> FetchedPage | None:
    """`webfetch.fetch()`, but a transport failure degrades to `None` instead of propagating --
    `crawl_site()` is itself an optional, best-effort enhancement (the caller always has the
    existing web-search path to fall back to), so no single candidate page -- including the
    homepage -- should be able to abort the whole attempt."""
    try:
        return await webfetch.fetch(url)
    except Exception as exc:
        _logger.debug("Failed to fetch %s while crawling: %s", url, exc, exc_info=exc)
        return None
