"""Redirect boundaries use real httpx control flow with no outbound network."""

import ipaddress
import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from nanobot.agent.tools.web import MAX_REDIRECTS, WebFetchConfig, WebFetchTool
from nanobot.security import network


@pytest.fixture
def mock_network(monkeypatch):
    requests = []
    answers = {"public.test": ["93.184.216.34"], "other.test": ["93.184.216.35"],
               "private.test": ["10.0.0.7"],
               "mixed.test": ["93.184.216.34", "10.0.0.7"]}
    routes = {}
    original_client = httpx.AsyncClient

    def resolve(host, port, *args, **kwargs):
        addresses = answers.get(host)
        if addresses is None:
            try:
                addresses = [str(ipaddress.ip_address(host))]
            except ValueError:
                raise socket.gaierror("synthetic unresolved hostname") from None
        return [(socket.AF_INET6 if ":" in addr else socket.AF_INET,
                 socket.SOCK_STREAM, 0, "", (addr, port or 0)) for addr in addresses]

    def response(request):
        requests.append(str(request.url))
        result = routes.get(str(request.url), httpx.Response(
            200, headers={"content-type": "text/plain"}, text="NORMAL17"))
        return result(request) if callable(result) else result

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return original_client(*args, transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr(network, "_allowed_networks", [])
    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests, answers, routes


async def fetch(tool, path, url):
    if path == "probe":
        return await tool.execute(url=url)
    return await tool._fetch_readability(url, "text", 50000)


@pytest.mark.parametrize("path", ["probe", "readability"])
@pytest.mark.parametrize("target", [
    "http://127.0.0.1/private", "http://10.0.0.7/private",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/private",
    "http://[fc00::7]/private", "http://[fe80::7]/private",
    "http://private.test/private", "http://mixed.test/private",
    "http://unresolved.test/private", "ftp://other.test/private",
])
async def test_every_redirect_is_blocked_before_transport(mock_network, path, target):
    requests, _, routes = mock_network
    routes["https://public.test/start"] = httpx.Response(
        302, headers={"location": "https://other.test/hop"})
    routes["https://other.test/hop"] = httpx.Response(307, headers={"location": target})
    tool = WebFetchTool()
    tool._fetch_jina = AsyncMock()
    if path == "probe":
        tool._fetch_readability = AsyncMock()

    result = json.loads(await fetch(tool, path, "https://public.test/start"))

    assert "error" in result
    assert requests == ["https://public.test/start", "https://other.test/hop"]
    tool._fetch_jina.assert_not_awaited()
    if path == "probe":
        tool._fetch_readability.assert_not_awaited()


@pytest.mark.parametrize("path", ["probe", "readability"])
async def test_initial_private_url_has_zero_requests(mock_network, path):
    requests, _, _ = mock_network
    tool = WebFetchTool()
    tool._fetch_jina = AsyncMock()
    result = json.loads(await fetch(tool, path, "http://169.254.169.254/private"))
    assert "error" in result
    assert requests == []
    tool._fetch_jina.assert_not_awaited()


@pytest.mark.parametrize("path", ["probe", "readability"])
async def test_public_relative_redirects_preserve_fetch(mock_network, path):
    requests, _, routes = mock_network
    routes["https://public.test/start"] = httpx.Response(301, headers={"location": "/hop"})
    routes["https://public.test/hop"] = httpx.Response(
        308, headers={"location": "//other.test/final"})
    tool = WebFetchTool(config=WebFetchConfig(use_jina_reader=False), user_agent="redirect-test")
    result = json.loads(await fetch(tool, path, "https://public.test/start"))
    assert result["finalUrl"] == "https://other.test/final"
    assert result["status"] == 200
    assert result["extractor"] == "raw"
    assert result["untrusted"] is True
    assert "NORMAL17" in result["text"]
    chain = ["https://public.test/start", "https://public.test/hop", "https://other.test/final"]
    assert requests == chain * (2 if path == "probe" else 1)


@pytest.mark.parametrize("path", ["probe", "readability"])
@pytest.mark.parametrize("target,allowed", [
    ("http://192.168.50.2/status", True), ("http://192.168.50.3/status", False),
    ("http://mixed.test/status", False),
])
async def test_redirect_respects_exact_whitelist(mock_network, path, target, allowed):
    requests, _, routes = mock_network
    network.configure_ssrf_whitelist(["192.168.50.2/32"])
    routes["https://public.test/start"] = httpx.Response(302, headers={"location": target})
    tool = WebFetchTool(config=WebFetchConfig(use_jina_reader=False))
    result = json.loads(await fetch(tool, path, "https://public.test/start"))
    assert ("error" not in result) is allowed
    assert requests.count(target) == ((2 if path == "probe" else 1) if allowed else 0)
    if allowed:
        assert result["finalUrl"] == target
        assert "NORMAL17" in result["text"]


@pytest.mark.parametrize("path", ["probe", "readability"])
async def test_public_redirect_loop_stays_bounded(mock_network, path):
    requests, _, routes = mock_network
    routes["https://public.test/loop"] = httpx.Response(302, headers={"location": "/loop"})
    tool = WebFetchTool(config=WebFetchConfig(use_jina_reader=False))
    result = json.loads(await fetch(tool, path, "https://public.test/loop"))
    assert "error" in result
    assert len(requests) == (MAX_REDIRECTS + 1) * (2 if path == "probe" else 1)


@pytest.mark.parametrize("path", ["probe", "readability"])
async def test_normal_redirected_image_preserves_content(mock_network, monkeypatch, path):
    requests, _, routes = mock_network
    routes["https://public.test/image"] = httpx.Response(302, headers={"location": "/final.png"})
    routes["https://public.test/final.png"] = httpx.Response(
        200, headers={"content-type": "image/png"}, content=b"SYNTHETIC-PNG")
    seen = []

    def image_blocks(raw, content_type, url, label):
        seen.append((raw, content_type, url))
        return [{"type": "image_url", "image_url": {"url": "synthetic-image"}}]

    monkeypatch.setattr("nanobot.agent.tools.web.build_image_content_blocks", image_blocks)
    result = await fetch(WebFetchTool(), path, "https://public.test/image")
    assert result[0]["type"] == "image_url"
    assert seen == [(b"SYNTHETIC-PNG", "image/png", "https://public.test/image")]
    assert requests == ["https://public.test/image", "https://public.test/final.png"]


@pytest.mark.parametrize("jina_succeeds", [True, False])
async def test_transport_failure_preserves_normal_fallback(mock_network, jina_succeeds):
    requests, _, routes = mock_network

    def fail_once(request):
        routes.pop(str(request.url))
        raise httpx.ConnectError("synthetic transport failure", request=request)

    routes["https://public.test/page"] = fail_once
    jina_url = "https://r.jina.ai/https://public.test/page"
    routes[jina_url] = (httpx.Response(200, json={"data": {"content": "NORMAL17"}})
                        if jina_succeeds else httpx.Response(429))
    result = json.loads(await WebFetchTool().execute(url="https://public.test/page"))
    assert result["extractor"] == ("jina" if jina_succeeds else "raw")
    assert result["untrusted"] is True
    assert "NORMAL17" in result["text"]
    assert requests == ["https://public.test/page", jina_url] + (
        [] if jina_succeeds else ["https://public.test/page"])


async def test_fallback_still_blocks_redirect_before_transport(mock_network):
    requests, _, routes = mock_network

    def fail_once(request):
        routes[str(request.url)] = httpx.Response(
            302, headers={"location": "http://private.test/secret"})
        raise httpx.ConnectError("synthetic transport failure", request=request)

    routes["https://public.test/page"] = fail_once
    jina_url = "https://r.jina.ai/https://public.test/page"
    routes[jina_url] = httpx.Response(429)
    result = json.loads(await WebFetchTool().execute(url="https://public.test/page"))
    assert "error" in result
    assert requests == ["https://public.test/page", jina_url, "https://public.test/page"]


async def test_probe_revalidates_initial_dns_before_transport(mock_network, monkeypatch):
    requests, _, _ = mock_network
    calls = []

    def resolve(host, port, *args, **kwargs):
        calls.append(host)
        addr = "93.184.216.34" if len(calls) == 1 else "10.0.0.7"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, port or 0))]

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    tool = WebFetchTool()
    tool._fetch_jina = AsyncMock()
    tool._fetch_readability = AsyncMock()
    result = json.loads(await tool.execute(url="https://public.test/page"))
    assert "error" in result
    assert calls == ["public.test", "public.test"]
    assert requests == []
    tool._fetch_jina.assert_not_awaited()
    tool._fetch_readability.assert_not_awaited()
