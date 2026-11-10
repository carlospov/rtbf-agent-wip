"""`test_localhost_configured_provider_makes_no_external_network_calls` (R18).

Runs a *real* local HTTP server (stdlib `http.server`, bound to `127.0.0.1`) as the stub LLM
endpoint, and monkeypatches `socket.getaddrinfo` — the DNS-resolution choke point every socket
connection (sync or async, `httpx` or otherwise) must pass through — to raise for any hostname
other than `localhost`/`127.0.0.1`/`::1`. This proves two things at once: `OpenAICompatibleAdapter`
completes a full request/response cycle against a `localhost` endpoint with zero outbound
internet access, and it never attempts to reach any other host while doing so (no hardcoded cloud
fallback, no accidental default endpoint).
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rtbf_agent.core.interfaces import LLMMessage, LLMRequest
from rtbf_agent.plugins.llm.openai_compatible import OpenAICompatibleAdapter

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _StubChatCompletionsHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib-mandated method name)
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)  # drain the request body
        body = json.dumps(
            {
                "model": "local-stub-model",
                "choices": [{"message": {"content": "offline response"}}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence stdlib's default stderr logging
        pass


@pytest.fixture()
def local_stub_server():
    server = HTTPServer(("127.0.0.1", 0), _StubChatCompletionsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def block_non_local_dns(monkeypatch):
    """Raise on any DNS resolution for a non-local hostname — the choke point both sync sockets
    and asyncio's event-loop-threaded resolver pass through, so this blocks outbound connections
    regardless of which transport internals `httpx` picks at runtime."""
    real_getaddrinfo = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if host not in ALLOWED_HOSTS:
            raise OSError(f"blocked: attempted DNS resolution for non-local host {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


async def test_localhost_configured_provider_makes_no_external_network_calls(
    local_stub_server, block_non_local_dns
) -> None:
    adapter = OpenAICompatibleAdapter(
        base_url=f"http://127.0.0.1:{local_stub_server}/v1", api_key="not-needed"
    )
    request = LLMRequest(
        messages=(LLMMessage(role="user", content="classify this"),), model="local-stub-model"
    )

    response = await adapter.complete(request)

    assert response.content == "offline response"
    assert response.model == "local-stub-model"
