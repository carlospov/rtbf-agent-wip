"""`HTTPWebFetchProvider` — the first (and, as of this writing, only) concrete `WebFetchProvider`.

## Why a direct HTTP fetch, not a third-party readability/content-extraction API

Real report: `RecipientDetector` found a site's own contact page in search results, but the
contact email lived in the page body, not in the search engine's own indexed snippet, so the LLM
never saw it (`core.interfaces.WebFetchProvider`'s docstring has the full story). The fix this
module implements is the plainest one that closes that gap: fetch the page directly over HTTP,
same "point it at the real thing yourself, no project-operated relay" shape this project already
uses for SMTP/LLM/search (R12/R13) — no third-party fetch/readability *service* to hand a URL to.

## HTML → text extraction, deliberately not a parsing library

Regex-based: strip `<script>`/`<style>` blocks, strip remaining tags, unescape entities, collapse
whitespace. The same call this project already made for `jurisdiction_detection/detector.py`'s
`_extract_json_object()` (manual brace-scanning instead of a JSON-repair dependency) — noisier
than a real readability extractor (no boilerplate/nav stripping), but sufficient to get real page
text -- an email address included -- in front of the LLM, with zero new parsing-library attack
surface for a narrow need.

## Safety: this fetches arbitrary URLs, unlike `SearXNGProvider`'s fixed, operator-configured
`base_url`

A URL here comes from a search result an upstream engine returned -- not a value the operator
typed into their own config. Three independent guards, in order:

1. **Scheme allowlist** -- only `http`/`https`; anything else (`file://`, `ftp://`, ...) is refused
   before any network access.
2. **SSRF guard on every hop** -- before connecting to a host (the original URL, and again for
   every redirect this adapter follows itself rather than delegating to httpx), the hostname is
   resolved and every resolved address is checked against `ipaddress`'s private/loopback/
   link-local/reserved/multicast/unspecified predicates, plus an explicit check for RFC 6598
   "Shared Address Space" (`100.64.0.0/10`) -- `ipaddress` has no built-in predicate for that
   block, but it's widely used *inside* cloud providers' internal networks, exactly the class of
   internal target this guard exists to block. This closes off "a crafted/compromised search
   result points at `169.254.169.254` (cloud metadata)/`localhost`/an internal service" --
   explicitly **best-effort**, not a hardened multi-tenant fetch proxy: it does not defend a
   sophisticated DNS-rebind between this check and the actual connection a moment later. That's a
   proportionate trade-off for a self-hosted, single-user tool fetching URLs sourced from its own
   configured search engine, not an adversarial multi-tenant fetch service.
3. **Redirects are followed manually, capped at `_MAX_REDIRECTS`** -- httpx's own automatic
   redirect-following would skip guard #2 for the redirect target; re-checking each hop here is
   the entire reason redirects aren't just left to `follow_redirects=True`.

Beyond that: a byte cap on the downloaded response (`_MAX_RESPONSE_BYTES`) so an oversized/hostile
page can't blow up memory, a `Content-Type` allowlist (only `text/html`/`text/plain` -- a PDF,
image, or video isn't something the extraction above can use), and a cap on the final extracted
text (`_MAX_FETCHED_TEXT_CHARS`, mirroring `_MAX_SNIPPET_CHARS` in both detector modules) so one
page can't dominate an LLM prompt's token budget (R19).

## Link extraction (for `plugins/webfetch/crawl.crawl_site()`)

`FetchedPage.links` -- the absolute hyperlink targets found on an HTML fetch, same dependency-free
regex approach as the text extraction above, capped at `_MAX_LINKS`. This is what lets a caller
navigate a site rather than only ever reading one page it already had a URL for: fetch a homepage,
follow its own links outward. Every followed link still goes through this same `fetch()` -- the
SSRF/scheme/redirect guards above apply identically whether a URL came from a search result or
from a page's own `<a href>`.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from rtbf_agent.core.interfaces import FetchedPage, WebFetchProvider

DEFAULT_TIMEOUT_SECONDS = 15.0
"""Shorter than `SearXNGProvider`'s 30s: this is a best-effort enrichment a detector may fetch
several of concurrently, not a required step -- a slow page shouldn't hold up the whole attempt
for long."""

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
"""Stop reading a response body past this many bytes -- bounds memory use against an oversized or
hostile page; the extraction step caps far below this anyway."""

_MAX_FETCHED_TEXT_CHARS = 3000
"""Cap on the final extracted text handed back in `FetchedPage.text` -- mirrors `_MAX_SNIPPET_CHARS`
in `plugins/jurisdiction_detection/detector.py`/`plugins/recipient_detection/detector.py` (R19:
one fetched page shouldn't dominate the prompt/token budget)."""

_ALLOWED_CONTENT_TYPES = ("text/html", "text/plain")

_MAX_REDIRECTS = 3
"""Redirects are followed manually (not via httpx's `follow_redirects=True`) so the SSRF guard
re-runs against each hop's actual target -- see module docstring."""

_SCRIPT_OR_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")

_LINK_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MAX_LINKS = 50
"""Cap on `FetchedPage.links` -- a caller navigating outward from a page (e.g.
`plugins/webfetch/crawl.crawl_site()`) only ever wants a handful of candidates, and an
attacker-controlled or link-heavy page shouldn't be able to make one fetch produce an unbounded
amount of follow-up work."""
_SKIPPED_LINK_SCHEMES = ("#", "javascript:", "mailto:", "tel:")


def _html_to_text(markup: str) -> str:
    """Best-effort plain text from `markup` -- see module docstring for why this is regex-based,
    not a parsing library. Not a readability extractor: nav/boilerplate isn't stripped, only tags
    themselves -- noisier than a real extractor, sufficient for an LLM to find real page content
    (e.g. a contact email) among the noise."""
    text = _SCRIPT_OR_STYLE_RE.sub(" ", markup)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def _extract_links(markup: str, page_url: str) -> tuple[str, ...]:
    """Absolute hyperlink targets found on `markup` (an HTML page fetched from `page_url`) --
    same regex-based, no-parser-library approach as `_html_to_text()`, for the same reason (see
    module docstring). Skips fragment-only (`#...`), `javascript:`, `mailto:`, and `tel:` targets
    -- none of those are pages a caller could usefully fetch next. Relative hrefs are resolved to
    absolute URLs via `urljoin(page_url, href)`. Capped at `_MAX_LINKS`, first-seen order,
    deduplicated."""
    links: list[str] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(markup):
        href = match.group(1).strip()
        if not href or href.lower().startswith(_SKIPPED_LINK_SCHEMES):
            continue
        absolute = urljoin(page_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
        if len(links) >= _MAX_LINKS:
            break
    return tuple(links)


def _strip_userinfo(url: str) -> str:
    """Drop any `user:pass@` embedded in `url`'s authority. code-reviewer finding: passed through
    unstripped, httpx would send that as an `Authorization: Basic` header to whatever host the URL
    (or a redirect target) resolves to -- a search result/redirect could smuggle credentials to an
    otherwise-allowed public host, and the raw URL (userinfo included) could also leak into a
    `RuntimeError` message or a returned `FetchedPage.url`. Low real-world risk here (URLs come
    from search results, not this project's own secrets), but stripping it is cheap and keeps
    "never send credentials embedded in a URL" true regardless of where a future URL comes from.

    code-reviewer finding (regression in the fix above): `urlsplit().hostname` always returns an
    IPv6 literal *without* its brackets (`"::1"`, not `"[::1]"`) -- reconstructing the netloc
    without re-adding them produced a malformed URL (`http://::1:8080/...`, ambiguous between host
    and port) that both broke the actual request *and* made the SSRF guard's next-iteration
    `urlsplit()` parse a different, wrong host than the one being connected to. Bracket the host
    back if it contains `:` (the one character never valid in an IPv4 literal/hostname, only in an
    IPv6 one) before reassembling.

    Found while writing a regression test for the fix above: `parsed.port` raises a bare
    `ValueError` (not caught anywhere) for a syntactically-malformed port (e.g.
    `https://host:abc/path`), which would otherwise crash `fetch()` before it ever reached the
    request/response handling that already has a defined "raises only `RuntimeError`" contract.
    A URL malformed enough to fail port parsing is also one `httpx.URL(...)` itself rejects with
    `httpx.InvalidURL` -- so on that failure this function gives up on reconstructing a full
    netloc and defers the "is this even fetchable" judgment to `fetch()`'s own `httpx.InvalidURL`
    handling rather than raising here itself.

    code-reviewer finding: that fallback originally returned `url` completely unchanged on a
    `ValueError`, which meant a URL with *both* embedded userinfo *and* a malformed port (e.g.
    `https://user:secret@host:abc/path`) kept its credentials all the way into the `RuntimeError`
    message `fetch()` raises once httpx rejects it -- exactly the leak this function exists to
    prevent, just reachable through the one path that wasn't actually stripping anything.
    `parsed.netloc` (the raw, unparsed authority string) is safe to read even when `.port` itself
    is what raised, so userinfo can still be dropped via a plain string split on the last `@`,
    without needing a valid port to reconstruct anything else.
    """
    parsed = urlsplit(url)
    if not parsed.hostname:
        return url
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        netloc_without_userinfo = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit(
            (parsed.scheme, netloc_without_userinfo, parsed.path, parsed.query, parsed.fragment)
        )
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _content_type_matches(header_value: str) -> bool:
    media_type = header_value.split(";", 1)[0].strip().lower()
    return media_type in _ALLOWED_CONTENT_TYPES


def _charset_from_content_type(header_value: str) -> str:
    for part in header_value.split(";")[1:]:
        part = part.strip()
        if part.lower().startswith("charset="):
            return part[len("charset=") :].strip("\"' ") or "utf-8"
    return "utf-8"


async def _default_resolve(host: str) -> list[str]:
    """Resolve `host` to its IP address(es) without blocking the event loop."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
"""RFC 6598 "Shared Address Space" (CGNAT) -- not covered by any of `ipaddress`'s built-in
`is_private`/`is_reserved`/etc. predicates (verified: `ip_address("100.64.0.1").is_private` is
`False`), but widely used *inside* cloud providers' internal networks for host-to-host/node-to-pod
traffic that is not meant to be internet-reachable -- exactly the class of internal target this
guard exists to block. code-reviewer finding."""


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # code-reviewer finding: an IPv4-mapped IPv6 address (`::ffff:x.x.x.x`) parses as an
    # IPv6Address whose own is_private/is_loopback/etc already correctly reflect the embedded
    # IPv4 address in this Python version (verified: `::ffff:127.0.0.1`.is_loopback is True) --
    # but our own RFC 6598 check below is guarded by `isinstance(ip, IPv4Address)`, which is
    # False for the mapped form, so `::ffff:100.64.0.1` would slip past *that* check specifically.
    # Recursing on `.ipv4_mapped` (when present) makes every check here, including future ones,
    # apply to the embedded address too -- not just papering over this one instance.
    mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
    if mapped is not None and _is_disallowed_ip(mapped):
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_ADDRESS_SPACE)
    )


class HTTPWebFetchProvider(WebFetchProvider):
    """Fetches a URL directly over HTTP(S) and extracts its plain text. The only concrete
    `WebFetchProvider` (R17-style minimum: exactly one required adapter to start, more addable
    without touching the ABC)."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        resolve_fn: Callable[[str], Awaitable[Sequence[str]]] | None = None,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        max_text_chars: int = _MAX_FETCHED_TEXT_CHARS,
    ) -> None:
        """`client` is an injection point for tests (an `httpx.AsyncClient` with a
        `httpx.MockTransport`) -- production callers omit it and a short-lived client is created
        per call, mirroring `SearXNGProvider`. `resolve_fn` is a second injection point specific to
        this adapter: the SSRF guard's hostname resolution must be mockable so tests never perform
        a real DNS lookup -- defaults to a real, non-blocking `getaddrinfo` resolution.
        """
        self._client = client
        self._timeout = timeout
        self._resolve_fn = resolve_fn if resolve_fn is not None else _default_resolve
        self._max_response_bytes = max_response_bytes
        self._max_text_chars = max_text_chars

    async def fetch(self, url: str) -> FetchedPage | None:
        """Fetch `url`, following redirects (each re-checked by the SSRF guard) up to
        `_MAX_REDIRECTS` times.

        Returns `None` for anything that isn't a usable 2xx text response (disallowed scheme/host,
        non-2xx status, non-text content type, too many redirects) -- never raises for those.
        Raises `RuntimeError` for a genuine transport failure (DNS/connection error, timeout) or
        for a malformed/unresolvable URL (`httpx.InvalidURL`, e.g. a bad redirect target).
        """
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            current_url = _strip_userinfo(url)
            for _ in range(_MAX_REDIRECTS + 1):
                parsed = urlsplit(current_url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    return None
                if await self._is_disallowed_host(parsed.hostname):
                    return None

                try:
                    async with client.stream(
                        "GET", current_url, follow_redirects=False
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                return None
                            current_url = _strip_userinfo(urljoin(current_url, location))
                            continue
                        if response.status_code // 100 != 2:
                            return None
                        content_type = response.headers.get("content-type", "")
                        if not _content_type_matches(content_type):
                            return None
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            remaining = self._max_response_bytes - len(body)
                            if remaining <= 0:
                                break
                            # Truncate the chunk itself, not just the loop -- a single chunk can
                            # exceed the whole cap (a hostile/misbehaving server has no obligation
                            # to chunk its response small), so only breaking afterward would let
                            # one oversized chunk defeat the cap entirely.
                            body.extend(chunk[:remaining])
                            if len(body) >= self._max_response_bytes:
                                break
                        charset = _charset_from_content_type(content_type)
                        try:
                            markup = bytes(body).decode(charset, errors="replace")
                        except LookupError:
                            markup = bytes(body).decode("utf-8", errors="replace")
                except (httpx.RequestError, httpx.InvalidURL) as exc:
                    # httpx.InvalidURL (code-reviewer finding): raised when constructing the
                    # request URL itself -- e.g. a redirect Location header that passes this
                    # module's own lax urlsplit() parsing but fails httpx's stricter URL rules --
                    # and is NOT a subclass of RequestError/HTTPError (verified against the
                    # installed httpx version), so it would otherwise propagate as a raw httpx
                    # exception, violating this method's documented "raises only RuntimeError"
                    # contract. Deliberately NOT broadened to httpx.HTTPError or httpx.StreamError:
                    # HTTPError wouldn't even catch InvalidURL (it isn't one), and StreamError (a
                    # separate, unrelated hierarchy under RuntimeError) signals a misuse of the
                    # streaming API -- a bug in this method's own code, not a transport failure --
                    # which should keep raising loudly rather than be folded into "could not
                    # fetch" as if it were routine.
                    raise RuntimeError(f"could not fetch {current_url}: {exc}") from exc

                text = _html_to_text(markup)[: self._max_text_chars]
                if not text:
                    return None
                media_type = content_type.split(";", 1)[0].strip().lower()
                links = _extract_links(markup, current_url) if media_type == "text/html" else ()
                return FetchedPage(url=current_url, text=text, links=links)
            return None  # too many redirects
        finally:
            if owns_client:
                await client.aclose()

    async def _is_disallowed_host(self, host: str) -> bool:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return _is_disallowed_ip(literal)

        try:
            addresses = await self._resolve_fn(host)
        except OSError:
            return True  # unresolvable -- treat as unfetchable, the safer default
        if not addresses:
            return True
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                return True
            if _is_disallowed_ip(ip):
                return True
        return False
