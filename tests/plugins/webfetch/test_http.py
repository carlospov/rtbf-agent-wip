"""Tests for `rtbf_agent.plugins.webfetch.http.HTTPWebFetchProvider` (R16, R20).

Uses `httpx.MockTransport` (no real network call, ever) for HTTP, and a fake `resolve_fn` (no real
DNS lookup, ever) for the SSRF guard's hostname resolution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from rtbf_agent.core.interfaces import FetchedPage, WebFetchProvider
from rtbf_agent.plugins.webfetch.http import HTTPWebFetchProvider


@asynccontextmanager
async def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield client


async def _public_resolve(host: str) -> list[str]:
    return ["93.184.216.34"]


def _unreachable_handler(request: httpx.Request) -> httpx.Response:
    """A transport that fails the test if it's ever actually asked to make a request -- used for
    cases the SSRF/scheme/hostname guard should short-circuit *before* any client call. code-
    reviewer finding: a test that omits `client=` still constructs a real (unused)
    `httpx.AsyncClient` under the hood, contradicting this module's "no real network call, ever"
    guarantee and silently depending on the current ordering of checks -- injecting this transport
    everywhere makes that guarantee explicit and enforced, not just currently-true by accident."""
    raise AssertionError(f"HTTPWebFetchProvider must not make a request for {request.url}")


async def test_http_webfetch_provider_implements_webfetchprovider_contract() -> None:
    # Uses the same safe-by-construction pattern as every other test in this file (code-reviewer
    # finding): this test never calls .fetch(), but constructing HTTPWebFetchProvider() with no
    # client/resolve_fn here would be a maintenance risk if a future edit added one -- consistency
    # with the rest of the file costs nothing.
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        assert isinstance(provider, WebFetchProvider)


async def test_fetch_strips_userinfo_from_the_url_before_requesting_and_returning() -> None:
    """code-reviewer finding: a URL with embedded `user:pass@` credentials must never reach httpx
    (which would send them as an `Authorization: Basic` header to whatever host it resolves to)
    nor leak into the returned `FetchedPage.url`."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://user:secret@example.invalid/contact")

    assert "user:secret" not in captured["url"]
    assert captured["authorization"] is None
    assert page is not None
    assert "user:secret" not in page.url
    assert page.url == "https://example.invalid/contact"


async def test_fetch_preserves_ipv6_brackets_when_stripping_userinfo() -> None:
    """code-reviewer finding: `urlsplit().hostname` returns an IPv6 literal without its brackets
    (`"::1"`, not `"[::1]"`) -- reassembling the URL without re-adding them produced a malformed
    URL (`http://::1:8080/...`, ambiguous between host and port) that broke both the actual
    request and the SSRF guard's next-iteration re-parse of the (now wrong) host."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        # A real, public (non-private/reserved) IPv6 literal -- the SSRF guard must let this
        # through so the test actually exercises the URL-reassembly bug, not the guard itself.
        page = await provider.fetch("http://user:secret@[2606:4700:4700::1111]:8080/path")

    assert captured["url"] == "http://[2606:4700:4700::1111]:8080/path"
    assert page is not None
    assert page.url == "http://[2606:4700:4700::1111]:8080/path"


async def test_fetch_extracts_plain_text_from_html() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><style>body{color:red}</style></head>"
                "<body><script>evil()</script>"
                "<p>Contact us at&nbsp;dpo@example.com for data requests.</p>"
                "</body></html>"
            ),
        )

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/contact")

    assert isinstance(page, FetchedPage)
    assert page.url == "https://example.invalid/contact"
    assert "dpo@example.com" in page.text
    assert "evil()" not in page.text
    assert "color:red" not in page.text
    assert "<p>" not in page.text


async def test_fetch_extracts_absolute_links_and_skips_non_navigable_ones() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><nav>"
                '<a href="/privacy">Privacy</a>'
                '<a href="https://example.invalid/contact?ref=footer">Contact</a>'
                '<a href="#top">Back to top</a>'
                '<a href="mailto:info@example.invalid">Email us</a>'
                '<a href="javascript:void(0)">Menu</a>'
                '<a href="tel:+1234567890">Call us</a>'
                '<a href="/privacy">Privacy again (duplicate)</a>'
                "</nav></body></html>"
            ),
        )

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/")

    assert page is not None
    assert page.links == (
        "https://example.invalid/privacy",
        "https://example.invalid/contact?ref=footer",
    )


async def test_fetch_caps_the_number_of_extracted_links() -> None:
    anchors = "".join(f'<a href="/page{i}">p{i}</a>' for i in range(200))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=f"<html>{anchors}</html>"
        )

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/")

    assert page is not None
    assert len(page.links) == 50


async def test_fetch_extracts_no_links_for_a_text_plain_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, text='see <a href="/privacy">here</a>'
        )

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/")

    assert page is not None
    assert page.links == ()


async def test_fetch_returns_none_for_disallowed_scheme() -> None:
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        assert await provider.fetch("file:///etc/passwd") is None


async def test_fetch_returns_none_for_a_url_with_no_hostname() -> None:
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        assert await provider.fetch("https:///path-only") is None


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata endpoint
        "0.0.0.0",
        "100.64.0.1",  # RFC 6598 Shared Address Space (CGNAT) -- code-reviewer finding: not
        # covered by ipaddress's is_private/is_reserved/etc, but used inside cloud providers'
        # internal networks.
    ],
)
async def test_fetch_refuses_a_literal_disallowed_ip(host: str) -> None:
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        assert await provider.fetch(f"http://{host}/") is None


async def test_fetch_refuses_a_literal_disallowed_ipv6_address() -> None:
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        assert await provider.fetch("http://[::1]/") is None


async def test_fetch_refuses_an_ipv4_mapped_ipv6_address_in_the_shared_address_space() -> None:
    """code-reviewer finding: Python's ipaddress predicates already correctly reflect the embedded
    IPv4 address for is_private/is_loopback/etc on an IPv4-mapped IPv6 literal (verified
    empirically), but this project's own RFC 6598 (CGNAT) check was guarded by
    `isinstance(ip, IPv4Address)`, which is False for the mapped form -- so `::ffff:100.64.0.1`
    specifically slipped past *that* one check."""
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        assert await provider.fetch("http://[::ffff:100.64.0.1]/") is None


@pytest.mark.parametrize(
    "host",
    [
        "::127.0.0.1",  # IPv4-compatible IPv6 (loopback embedded)
        "::169.254.169.254",  # IPv4-compatible IPv6 (cloud metadata embedded)
        "::8.8.8.8",  # IPv4-compatible IPv6 wrapping an otherwise-public IPv4 address
    ],
)
async def test_fetch_refuses_ipv4_compatible_ipv6_addresses(host: str) -> None:
    """A code-reviewer comment claimed IPv4-compatible IPv6 addresses (`::x.x.x.x`, distinct from
    the IPv4-*mapped* `::ffff:x.x.x.x` form) bypass every guard since `.ipv4_mapped` is `None` for
    them. Verified empirically before acting: this Python version's `ipaddress` module already
    marks the *entire* `::/96` range `is_reserved=True` (confirmed for both a loopback-derived
    address and a genuinely public-looking one like `::8.8.8.8`), so this was a false positive --
    already caught by the existing `is_reserved` check, not bypassed. This test locks that in."""
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        assert await provider.fetch(f"http://[{host}]/") is None


async def test_fetch_refuses_a_hostname_that_resolves_to_a_private_address() -> None:
    async def resolve_to_internal(host: str) -> list[str]:
        return ["10.0.0.5"]

    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=resolve_to_internal)
        assert await provider.fetch("https://internal.example/") is None


async def test_fetch_returns_none_when_resolution_fails() -> None:
    async def raising_resolve(host: str) -> list[str]:
        raise OSError("name resolution failed")

    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=raising_resolve)
        assert await provider.fetch("https://nowhere.invalid/") is None


async def test_fetch_follows_a_redirect_and_re_checks_the_ssrf_guard() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://example.invalid/old":
            return httpx.Response(302, headers={"location": "https://example.invalid/new"})
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="the real content lives here",
        )

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/old")

    assert page is not None
    assert page.url == "https://example.invalid/new"
    assert "the real content lives here" in page.text


async def test_fetch_refuses_a_redirect_to_a_disallowed_host() -> None:
    """The SSRF guard must re-run against the redirect *target*, not just the original URL --
    otherwise a compromised/malicious page could redirect an approved request at an internal
    service."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/redirects-to-metadata")

    assert page is None


async def test_fetch_gives_up_after_too_many_redirects() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            302, headers={"location": f"https://example.invalid/{calls['count']}"}
        )

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/0")

    assert page is None


async def test_fetch_returns_none_for_a_non_2xx_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"}, text="not found")

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/missing")

    assert page is None


async def test_fetch_returns_none_for_a_disallowed_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/doc.pdf")

    assert page is None


async def test_fetch_caps_the_downloaded_response_size() -> None:
    huge_body = ("<p>" + "a" * 5000 + "</p>").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=huge_body)

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(
            client=client, resolve_fn=_public_resolve, max_response_bytes=100, max_text_chars=10_000
        )
        page = await provider.fetch("https://example.invalid/huge")

    assert page is not None
    # At most 100 raw bytes were ever read (well under the full 5000 'a's) -- decoding can only
    # shrink byte count into character count, never grow it, so this bounds the text length too.
    assert len(page.text) <= 100


async def test_fetch_caps_the_extracted_text_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 10_000)

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(
            client=client, resolve_fn=_public_resolve, max_text_chars=50
        )
        page = await provider.fetch("https://example.invalid/long")

    assert page is not None
    assert len(page.text) == 50


async def test_fetch_returns_none_for_empty_extracted_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<script>x()</script>"
        )

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        page = await provider.fetch("https://example.invalid/empty")

    assert page is None


async def test_fetch_raises_runtimeerror_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _make_client(handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        with pytest.raises(RuntimeError, match="could not fetch"):
            await provider.fetch("https://example.invalid/")


async def test_fetch_raises_runtimeerror_not_a_raw_httpx_invalidurl_for_a_malformed_port() -> None:
    """code-reviewer finding: `httpx.InvalidURL` is not a subclass of `httpx.RequestError`/
    `httpx.HTTPError`, so it would otherwise propagate as a raw httpx exception, violating this
    method's documented "raises only RuntimeError" contract -- e.g. a redirect Location header (or
    any URL) with a syntactically invalid port."""
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        with pytest.raises(RuntimeError, match="could not fetch"):
            await provider.fetch("https://example.invalid:abc/path")


async def test_fetch_strips_userinfo_even_when_the_port_is_also_malformed() -> None:
    """code-reviewer finding: a URL with *both* embedded userinfo and a malformed port previously
    kept its credentials all the way into the RuntimeError message -- the ValueError fallback for
    the malformed-port case returned the URL completely unchanged, the one path that wasn't
    actually stripping anything."""
    async with _make_client(_unreachable_handler) as client:
        provider = HTTPWebFetchProvider(client=client, resolve_fn=_public_resolve)
        with pytest.raises(RuntimeError) as exc_info:
            await provider.fetch("https://user:secret@example.invalid:abc/path")

    assert "user:secret" not in str(exc_info.value)
