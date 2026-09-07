from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import httpx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from win32_mcp_server.config import RemoteConfig, _remote_config_from_env
from win32_mcp_server.server import _BearerAuthASGIMiddleware, app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

_INIT_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def test_remote_config_path_gets_leading_and_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIN32_MCP_REMOTE_PATH", "mcp")
    assert _remote_config_from_env(RemoteConfig()).path == "/mcp/"

    monkeypatch.setenv("WIN32_MCP_REMOTE_PATH", "/custom")
    assert _remote_config_from_env(RemoteConfig()).path == "/custom/"

    monkeypatch.setenv("WIN32_MCP_REMOTE_PATH", "/already/slashed/")
    assert _remote_config_from_env(RemoteConfig()).path == "/already/slashed/"


@contextlib.asynccontextmanager
async def _running_mcp_app(expected_header: str | None) -> AsyncIterator[httpx.AsyncClient]:
    """Build the same Starlette + StreamableHTTP wiring async_main_http() uses."""
    session_manager = StreamableHTTPSessionManager(app=app)
    mcp_asgi_app = _BearerAuthASGIMiddleware(session_manager.handle_request, expected_header)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    starlette_app = Starlette(routes=[Mount("/mcp/", app=mcp_asgi_app)], lifespan=lifespan)
    transport = httpx.ASGITransport(app=starlette_app)

    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        contextlib.AsyncExitStack() as stack,
    ):
        await stack.enter_async_context(session_manager.run())
        yield client


def test_bearer_auth_middleware_rejects_missing_or_wrong_token() -> None:
    async def scenario() -> None:
        async with _running_mcp_app("Bearer secret") as client:
            resp = await client.post("/mcp/", json=_INIT_BODY, headers=_INIT_HEADERS)
            assert resp.status_code == 401

            wrong_headers = {**_INIT_HEADERS, "Authorization": "Bearer wrong"}
            resp = await client.post("/mcp/", json=_INIT_BODY, headers=wrong_headers)
            assert resp.status_code == 401

    asyncio.run(scenario())


def test_bearer_auth_middleware_allows_correct_token_and_reaches_mcp_server() -> None:
    async def scenario() -> None:
        async with _running_mcp_app("Bearer secret") as client:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.0.1"},
                },
            }
            headers = {**_INIT_HEADERS, "Authorization": "Bearer secret"}
            resp = await client.post("/mcp/", json=body, headers=headers)
            assert resp.status_code == 200
            assert "win32-inspector" in resp.text

    asyncio.run(scenario())


def test_bearer_auth_middleware_allows_everything_when_no_token_configured() -> None:
    async def scenario() -> None:
        async with _running_mcp_app(None) as client:
            resp = await client.post("/mcp/", json=_INIT_BODY, headers=_INIT_HEADERS)
            assert resp.status_code != 401

    asyncio.run(scenario())
