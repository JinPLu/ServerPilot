from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from serverpilot import daemon
from serverpilot.client import (
    BrokerClient,
    control_plane_async_httpx_client,
    control_plane_http_request,
)


class _LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health/live":
            body = b'{"status":"live","schema_version":"v1"}'
        elif self.path == "/api/v1/snapshot":
            body = b'{"schema_version":"v1","data":[]}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _PoisonProxyHandler(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:
        type(self).hits += 1
        body = b"proxy"
        self.send_response(502)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


@pytest.fixture
def loopback_url() -> str:
    server, url = _serve(_LoopbackHandler)
    try:
        yield url
    finally:
        server.shutdown()


@pytest.fixture
def poison_proxy_url() -> str:
    _PoisonProxyHandler.hits = 0
    server, url = _serve(_PoisonProxyHandler)
    try:
        yield url
    finally:
        server.shutdown()


def _poison_proxy_env(monkeypatch: pytest.MonkeyPatch, proxy_url: str) -> None:
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("ALL_PROXY", proxy_url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def test_async_control_plane_client_disables_trust_env(
    monkeypatch: pytest.MonkeyPatch,
    poison_proxy_url: str,
) -> None:
    _poison_proxy_env(monkeypatch, poison_proxy_url)

    async def check() -> None:
        async with control_plane_async_httpx_client(timeout=2.0) as client:
            assert client.trust_env is False

    asyncio.run(check())


def test_loopback_clients_ignore_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
    loopback_url: str,
    poison_proxy_url: str,
) -> None:
    _poison_proxy_env(monkeypatch, poison_proxy_url)

    live = control_plane_http_request("GET", f"{loopback_url}/health/live", timeout=2)
    assert live.status_code == 200
    assert live.json() == {"status": "live", "schema_version": "v1"}

    snapshot = BrokerClient(loopback_url).get("/api/v1/snapshot")
    assert snapshot == {"schema_version": "v1", "data": []}

    probed = daemon._probe_json(loopback_url, "/health/live")
    assert probed == {"status": "live", "schema_version": "v1"}
    assert _PoisonProxyHandler.hits == 0

    intercepted = httpx.get(f"{loopback_url}/health/live", timeout=2)
    assert intercepted.status_code == 502
    assert _PoisonProxyHandler.hits == 1
