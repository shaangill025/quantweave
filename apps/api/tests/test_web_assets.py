"""The API serving the built web app from `Settings.web_dir` (T060; owner decision
109): same origin, API routes first, no listing, no escape from the directory, and
the Envelope's origin check and headers on every static response. The web
directory is a SYNTHETIC stand-in for `vite build` output; no database is used
(no route here reaches one)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from qw_api import Settings, create_app
from qw_api.security import LocalPasswordAuthenticator
from starlette.types import Message

ORIGIN = "https://testserver"
KEY = b"SYNTHETIC-server-key-0123456789abcdef"
INDEX = "<!doctype html><title>SYNTHETIC</title>"


@pytest.fixture
def web(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "app.js").write_text("export {};")
    (root / "empty").mkdir()
    (root / "api" / "v1").mkdir(parents=True)  # must never shadow or answer an API path
    (root / "api" / "v1" / "nope").write_text("SYNTHETIC-shadow")
    (root / "api" / "x.txt").write_text("SYNTHETIC-shadow")
    (root / "apix").symlink_to(root / "api")  # a second name for web/api
    (tmp_path / "secret.txt").write_text("SYNTHETIC-outside")
    (root / "leak.txt").symlink_to(tmp_path / "secret.txt")
    (root / "alias.js").symlink_to(root / "assets" / "app.js")
    return root


def client(web_dir: Path | None, origin: str | None = ORIGIN) -> TestClient:
    settings = Settings(
        frozenset({ORIGIN}), KEY, web_dir=None if web_dir is None else str(web_dir)
    )
    app = create_app(settings, lambda: None, LocalPasswordAuthenticator())  # type: ignore[arg-type, return-value]
    headers = {} if origin is None else {"Origin": origin}
    return TestClient(app, ORIGIN, headers=headers, raise_server_exceptions=False)


def raw_get(c: TestClient, path: str) -> tuple[int, bytes]:
    """GET with exactly this (already decoded) ASGI path: an HTTP client would
    normalise `//`, `.` and `..` away, a server in front of the API may not."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "https", "path": path, "root_path": "",
             "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 1),
             "server": ("testserver", 443)}  # fmt: skip

    async def call() -> None:
        await c.app(scope, receive, send)

    asyncio.run(call())
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


def test_without_a_web_dir_nothing_is_served(web: Path) -> None:
    c = client(None)
    for path in ("/", "/index.html", "/assets/app.js"):
        r = c.get(path)
        assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_the_built_app_is_served_with_the_envelope_headers(web: Path) -> None:
    c = client(web)
    for path in ("/", "/index.html"):
        r = c.get(path)
        assert (r.status_code, r.text) == (200, INDEX)
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-store"
        assert len(r.headers["x-correlation-id"]) == 32
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert c.head("/").status_code == 200
    js = c.get("/assets/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert c.get("/alias.js").status_code == 200  # a symlink inside the directory


def test_api_routes_take_precedence(web: Path) -> None:
    c = client(web)
    live = c.get("/api/v1/health/live")
    assert (live.status_code, live.json()) == (200, {"status": "healthy"})
    assert "x-frame-options" not in live.headers  # static headers stay static
    # Nothing under web/api is served, however the path is spelled.
    for path in ("/api/v1/nope", "/api/v1", "/api", "/api/", "/api/x.txt",
                 "/assets/%2e%2e/api/x.txt", "/assets/..%2fapi/x.txt",
                 "/apix/x.txt", "/apix/v1/nope"):  # fmt: skip
        r = c.get(path)
        assert r.status_code == 404 and r.json()["code"] == "not_found", path
        assert "SYNTHETIC-shadow" not in r.text
    for path in ("/assets/../api/v1/nope", "//api/v1/nope", "/./api/v1/nope",
                 "//api/x.txt", "/assets/../api/x.txt", "///api//v1//nope",
                 "/assets/./../apix/x.txt"):  # fmt: skip
        status, body = raw_get(c, path)
        assert (status, json.loads(body)["code"]) == (404, "not_found"), path
    assert raw_get(c, "/assets/../index.html") == (200, INDEX.encode())
    wrong = c.delete("/api/v1/health/live")  # a known path keeps its 405
    assert (wrong.status_code, wrong.json()["code"]) == (405, "method_not_allowed")


def test_no_listing_no_escape_and_no_writes(web: Path) -> None:
    c = client(web)
    for path in ("/assets/", "/empty/", "/empty", "/leak.txt", "/missing.js",
                 "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt",
                 "/assets/%2e%2e/%2e%2e/secret.txt", "/%00"):  # fmt: skip
        r = c.get(path)
        assert r.status_code == 404, path
        assert "SYNTHETIC-outside" not in r.text and "app.js" not in r.text
    # State-changing methods: the origin check still runs first, then no route.
    assert c.post("/").status_code == 404 and c.put("/index.html").status_code == 404
    foreign = client(web, origin="https://evil.example")
    assert foreign.post("/").json()["code"] == "origin_rejected"
    assert client(web, origin=None).post("/index.html").status_code == 403


def test_web_dir_must_be_absolute() -> None:
    for bad in ("web", "./web", ""):
        with pytest.raises(ValueError, match="web_dir"):
            Settings(frozenset({ORIGIN}), KEY, web_dir=bad)
