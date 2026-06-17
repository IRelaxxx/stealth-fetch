import time

import pytest
from mcp.shared.exceptions import McpError

from mcp_server_stealth_fetch import server as srv
from mcp_server_stealth_fetch.server import ServerConfig, StealthFetchState


def test_netloc_key_from_fetch_url():
    assert srv.netloc_key_from_fetch_url("https://Example.com/path") == "example.com"
    assert srv.netloc_key_from_fetch_url("https://x.org:8443/") == "x.org:8443"


def test_netloc_key_from_fetch_url_requires_host():
    with pytest.raises(ValueError):
        srv.netloc_key_from_fetch_url("/relative")


def test_netloc_key_from_domain_input():
    assert srv.netloc_key_from_domain_input("Example.COM") == "example.com"
    assert srv.netloc_key_from_domain_input("https://Foo.bar:9000/x") == "foo.bar:9000"
    assert srv.netloc_key_from_domain_input("baz.test") == "baz.test"


@pytest.mark.asyncio
async def test_same_host_reuses_one_session(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.05)
    created: list[FakeSession] = []

    def factory(**kwargs):
        s = FakeSession(**kwargs)
        created.append(s)
        return s

    state = StealthFetchState(ServerConfig(), session_factory=factory)
    await state.fetch("https://a.example/page1", 5000, 0, True)
    await state.fetch("https://a.example/page2", 5000, 0, True)
    assert len(created) == 1
    assert len(created[0].calls) == 2


@pytest.mark.asyncio
async def test_different_hosts_separate_sessions(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.05)
    created: list[FakeSession] = []

    def factory(**kwargs):
        s = FakeSession(**kwargs)
        created.append(s)
        return s

    state = StealthFetchState(ServerConfig(), session_factory=factory)
    await state.fetch("https://a.example/", 5000, 0, True)
    await state.fetch("https://b.example/", 5000, 0, True)
    assert len(created) == 2


@pytest.mark.asyncio
async def test_global_fetch_spacing(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.25)
    state = StealthFetchState(ServerConfig(), session_factory=lambda **k: FakeSession(**k))
    await state.fetch("https://a.example/", 5000, 0, True)
    t0 = time.monotonic()
    await state.fetch("https://b.example/", 5000, 0, True)
    assert time.monotonic() - t0 >= 0.24


@pytest.mark.asyncio
async def test_end_session_then_new_session(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.05)
    created: list[FakeSession] = []

    def factory(**kwargs):
        s = FakeSession(**kwargs)
        created.append(s)
        return s

    state = StealthFetchState(ServerConfig(), session_factory=factory)
    await state.fetch("https://a.example/", 5000, 0, True)
    assert len(created) == 1
    msg = await state.end_session("a.example")
    assert "Closed" in msg
    await state.fetch("https://a.example/other", 5000, 0, True)
    assert len(created) == 2


@pytest.mark.asyncio
async def test_end_session_idempotent():
    state = StealthFetchState(ServerConfig(), session_factory=lambda **k: FakeSession(**k))
    msg = await state.end_session("missing.example")
    assert "No session" in msg


@pytest.mark.asyncio
async def test_end_session_clears_cache_without_http_session():
    state = StealthFetchState(ServerConfig(), session_factory=lambda **k: FakeSession(**k))
    state._page_cache[("https://a.example/x", False)] = ("hello", "")
    msg = await state.end_session("a.example")
    assert "No session" in msg
    assert ("https://a.example/x", False) not in state._page_cache


@pytest.mark.asyncio
async def test_pagination_second_chunk_uses_cache_no_second_http(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.05)
    monkeypatch.setattr(
        srv,
        "extract_content_from_html",
        lambda html: "0123456789" * 2000,
    )
    calls: list[str] = []

    class CountingSession(FakeSession):
        async def get_async(self, url, **kwargs):
            calls.append(url)
            return FakeResp()

    state = StealthFetchState(ServerConfig(), session_factory=lambda **k: CountingSession(**k))
    await state.fetch("https://a.example/wiki", 5000, 0, False)
    assert len(calls) == 1
    _, body = await state.fetch("https://a.example/wiki", 5000, 5000, False)
    assert len(calls) == 1
    assert body[0] == "0"
    assert len(body) >= 5000


@pytest.mark.asyncio
async def test_pagination_without_cache_raises(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.05)
    state = StealthFetchState(ServerConfig(), session_factory=lambda **k: FakeSession(**k))
    with pytest.raises(McpError, match="No cached document"):
        await state.fetch("https://a.example/", 5000, 100, False)


@pytest.mark.asyncio
async def test_end_session_clears_page_cache(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.05)
    monkeypatch.setattr(
        srv,
        "extract_content_from_html",
        lambda html: "x" * 12000,
    )
    state = StealthFetchState(ServerConfig(), session_factory=lambda **k: FakeSession(**k))
    await state.fetch("https://a.example/p", 5000, 0, False)
    await state.fetch("https://a.example/p", 5000, 5000, False)
    await state.end_session("a.example")
    with pytest.raises(McpError, match="No cached document"):
        await state.fetch("https://a.example/p", 5000, 5000, False)


@pytest.mark.asyncio
async def test_end_session_does_not_advance_fetch_spacing(monkeypatch):
    monkeypatch.setattr(srv, "MIN_SECONDS_BETWEEN_FETCHES", 0.3)
    state = StealthFetchState(ServerConfig(), session_factory=lambda **k: FakeSession(**k))
    await state.fetch("https://a.example/", 5000, 0, True)
    await state.end_session("a.example")
    t0 = time.monotonic()
    await state.fetch("https://b.example/", 5000, 0, True)
    assert time.monotonic() - t0 >= 0.28


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[str] = []

    async def get_async(self, url, **kwargs):
        self.calls.append(url)
        return FakeResp()

    def close(self):
        pass


class FakeResp:
    status_code = 200
    text = "<html><body><p>x</p></body></html>"
    headers = {"content-type": ["text/html"]}
