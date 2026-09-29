import http.server
import threading

import pytest

from hi import fetcher
from hi.db import pool


def _start_server(routes: dict) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            handler = routes.get(self.path)
            if handler is None:
                self.send_response(404)
                self.end_headers()
                return
            handler(self)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"127.0.0.1:{server.server_address[1]}"


def _text_route(body: str, status: int = 200):
    def handler(req: http.server.BaseHTTPRequestHandler) -> None:
        data = body.encode()
        req.send_response(status)
        req.send_header("Content-Type", "text/html")
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    return handler


def _redirect_route(location: str):
    def handler(req: http.server.BaseHTTPRequestHandler) -> None:
        req.send_response(302)
        req.send_header("Location", location)
        req.end_headers()

    return handler


def _allow_all_robots(req: http.server.BaseHTTPRequestHandler) -> None:
    _text_route("User-agent: *\nAllow: /")(req)


def _enable_policy(db_conn, domain: str, *, respect_robots: bool = False) -> None:
    db_conn.execute(
        "insert into source_policy (domain, adapter, enabled, respect_robots, rate_limit_rps) "
        "values (%s, 'test', true, %s, 1000)",
        (domain, respect_robots),
    )


@pytest.mark.asyncio
async def test_policy_denied_without_enabled_row(db_conn):
    server, domain = _start_server({"/x": _text_route("hello")})
    try:
        with pytest.raises(fetcher.PolicyDenied):
            await fetcher.fetch(f"http://{domain}/x", adapter="test")
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_robots_denied(db_conn):
    server, domain = _start_server(
        {
            "/robots.txt": _text_route("User-agent: *\nDisallow: /secret"),
            "/secret": _text_route("should never be served"),
        }
    )
    try:
        _enable_policy(db_conn, domain, respect_robots=True)
        with pytest.raises(fetcher.RobotsDenied):
            await fetcher.fetch(f"http://{domain}/secret", adapter="test")
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_cache_hit_skips_second_request(db_conn):
    calls = {"n": 0}

    def counted(req: http.server.BaseHTTPRequestHandler) -> None:
        calls["n"] += 1
        _text_route("hello")(req)

    server, domain = _start_server({"/x": counted})
    try:
        _enable_policy(db_conn, domain)
        first = await fetcher.fetch(f"http://{domain}/x", adapter="test")
        second = await fetcher.fetch(f"http://{domain}/x", adapter="test")

        assert calls["n"] == 1
        assert first.from_cache is False
        assert second.from_cache is True
        assert second.text == "hello"
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_retry_then_succeed(db_conn):
    calls = {"n": 0}

    def flaky(req: http.server.BaseHTTPRequestHandler) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            _text_route("boom", status=500)(req)
        else:
            _text_route("ok")(req)

    server, domain = _start_server({"/x": flaky})
    try:
        _enable_policy(db_conn, domain)
        result = await fetcher.fetch(f"http://{domain}/x", adapter="test")
        assert result.status == 200
        assert result.text == "ok"
        assert calls["n"] == 3
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_4xx_is_not_retried(db_conn):
    calls = {"n": 0}

    def not_found(req: http.server.BaseHTTPRequestHandler) -> None:
        calls["n"] += 1
        _text_route("nope", status=404)(req)

    server, domain = _start_server({"/x": not_found})
    try:
        _enable_policy(db_conn, domain)
        result = await fetcher.fetch(f"http://{domain}/x", adapter="test")
        assert result.status == 404
        assert calls["n"] == 1
    finally:
        server.shutdown()


def test_redact_url_strips_credentials():
    redacted = fetcher.redact_url("https://serpapi.com/search.json?q=python&api_key=SECRET123")
    assert "SECRET123" not in redacted
    assert "q=python" in redacted


@pytest.mark.asyncio
async def test_api_key_is_never_written_to_the_fetch_table(db_conn):
    server, domain = _start_server({"/search.json": _text_route('{"ok": true}')})
    try:
        _enable_policy(db_conn, domain)
        await fetcher.fetch(
            f"http://{domain}/search.json?q=python&api_key=SUPERSECRET", adapter="test"
        )

        stored = db_conn.execute("select url from \"fetch\"").fetchall()
        assert stored, "expected a fetch row"
        assert not any("SUPERSECRET" in row[0] for row in stored)
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_cross_domain_redirect_rechecks_policy(db_conn):
    server_b, domain_b = _start_server({"/target": _text_route("should never be served")})
    try:
        server_a, domain_a = _start_server({"/go": _redirect_route(f"http://{domain_b}/target")})
        try:
            _enable_policy(db_conn, domain_a)
            # domain_b deliberately has no source_policy row.
            with pytest.raises(fetcher.PolicyDenied):
                await fetcher.fetch(f"http://{domain_a}/go", adapter="test")
        finally:
            server_a.shutdown()
    finally:
        server_b.shutdown()
