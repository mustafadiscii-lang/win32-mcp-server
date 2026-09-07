"""
MCP Server for Windows UI Inspection and Control - v2.6

Enterprise-grade automation server with 53 tools for screen capture, OCR,
mouse/keyboard control, window management, process control, UI Automation,
and intelligent high-level automation (click_text, wait_for_text, fill_field, etc.).

Author: Randy Northrup
GitHub: https://github.com/RandyNorthrup/win32-mcp-server
"""

import argparse
import asyncio
import contextlib
import json
import logging
import platform
import secrets
import sys
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from mcp.server import Server
from mcp.types import ImageContent, TextContent, Tool

from . import __version__
from .config import config
from .registry import registry
from .utils.security import redact_arguments, safe_json_dumps

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("win32-mcp")

# ---------------------------------------------------------------------------
# Import all tool modules to trigger registration
# ---------------------------------------------------------------------------

from . import tools  # noqa: F401  — registers all tools

# ---------------------------------------------------------------------------
# Server-level tools (health_check is registered here)
# ---------------------------------------------------------------------------


@registry.register(
    "health_check",
    "Verify all dependencies and report server status",
    {
        "type": "object",
        "properties": {},
    },
)
async def handle_health_check(arguments: dict[str, Any]) -> dict[str, Any]:
    from .utils.coordinates import get_all_monitors, get_scaling_factor, get_system_dpi
    from .utils.imaging import check_tesseract

    status: dict[str, Any] = {
        "server_version": __version__,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "security_profile": config.security.profile,
        "result_envelope": config.result_envelope,
        "coordinate_validation": config.validate_coordinates,
        "pyautogui_failsafe": config.automation.pyautogui_failsafe,
        "dry_run": config.security.dry_run,
        "tool_policy": {
            "allowed_tools": len(config.security.allowed_tools),
            "blocked_tools": len(config.security.blocked_tools),
            "allowed_commands": len(config.security.allowed_commands),
            "blocked_commands": len(config.security.blocked_commands),
            "confirmation_token_required": bool(config.security.confirmation_token),
        },
        "capture_defaults": {
            "format": config.capture.default_format,
            "quality": config.capture.default_quality,
            "scale": config.capture.default_scale,
        },
        "ocr_defaults": {
            "lang": config.ocr.lang,
            "preprocess": config.ocr.preprocess_mode,
            "tesseract_path_configured": bool(config.ocr.tesseract_path),
        },
    }

    # DPI / Scaling
    try:
        dpi = get_system_dpi()
        status["dpi"] = dpi
        status["display_scaling"] = f"{get_scaling_factor() * 100:.0f}%"
    except Exception as exc:
        status["dpi_error"] = str(exc)

    # Monitors
    try:
        monitors = get_all_monitors()
        status["monitor_count"] = len(monitors)
        if monitors:
            primary = monitors[0]
            status["primary_resolution"] = f"{primary['width']}x{primary['height']}"
    except Exception as exc:
        status["monitor_error"] = str(exc)

    # Tesseract OCR
    ok, msg = check_tesseract()
    status["tesseract"] = {"installed": ok, "info": msg}

    # Dependencies
    deps = {}
    for mod_name in [
        "mcp",
        "mss",
        "PIL",
        "pyautogui",
        "pygetwindow",
        "pyperclip",
        "pytesseract",
        "psutil",
        "numpy",
        "uiautomation",
    ]:
        try:
            mod = __import__(mod_name)
            ver = getattr(mod, "__version__", getattr(mod, "VERSION", "installed"))
            deps[mod_name] = str(ver)
        except ImportError:
            deps[mod_name] = "NOT INSTALLED"
    status["dependencies"] = deps

    # Rapidfuzz (optional)
    try:
        import rapidfuzz

        deps["rapidfuzz"] = rapidfuzz.__version__
    except ImportError:
        deps["rapidfuzz"] = "not installed (using difflib fallback)"

    # Tool count
    status["registered_tools"] = len(registry.tool_names)
    status["tools"] = registry.tool_names

    return status


# ---------------------------------------------------------------------------
# MCP Server Instance
# ---------------------------------------------------------------------------

app = Server("win32-inspector")


@app.list_tools()  # type: ignore[untyped-decorator, no-untyped-call]
async def list_tools() -> list[Tool]:
    """Return all registered tool definitions."""
    return registry.get_tools()


@app.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: Any) -> list[TextContent | ImageContent]:
    """Dispatch a tool call through the registry."""
    args = arguments if isinstance(arguments, dict) else {}
    logger.debug("Tool call: %s(%s)", name, safe_json_dumps(redact_arguments(name, args), max_chars=500))
    return await registry.dispatch(name, args)


# ---------------------------------------------------------------------------
# Entry Points
# ---------------------------------------------------------------------------


async def async_main() -> None:
    """Async entry point — runs the MCP server over stdio."""
    from mcp.server.stdio import stdio_server

    logger.info("win32-mcp-server v%s starting (%d tools registered)", __version__, len(registry.tool_names))

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


class _BearerAuthASGIMiddleware:
    """Rejects HTTP requests that don't carry the configured bearer token.

    Wraps the raw ASGI app instead of using Starlette's BaseHTTPMiddleware so the
    underlying Streamable HTTP transport can stream SSE responses untouched.
    """

    def __init__(self, asgi_app: "ASGIApp", expected_header: str | None) -> None:
        self._asgi_app = asgi_app
        self._expected_header = expected_header

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope.get("type") != "http" or self._expected_header is None:
            await self._asgi_app(scope, receive, send)
            return

        from starlette.responses import JSONResponse

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode("latin-1")
        if not secrets.compare_digest(provided, self._expected_header):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self._asgi_app(scope, receive, send)


async def async_main_http() -> None:
    """Async entry point — serves Streamable HTTP for remote clients (e.g. a phone MCP app)."""
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    remote = config.remote
    if not remote.auth_token and not remote.allow_no_auth:
        msg = (
            "Refusing to start the remote HTTP transport without an auth token. "
            "Set WIN32_MCP_REMOTE_TOKEN, or WIN32_MCP_REMOTE_ALLOW_NO_AUTH=true "
            "to knowingly accept the risk on a trusted network."
        )
        raise RuntimeError(msg)

    session_manager = StreamableHTTPSessionManager(app=app)
    expected_header = f"Bearer {remote.auth_token}" if remote.auth_token else None
    mcp_asgi_app = _BearerAuthASGIMiddleware(session_manager.handle_request, expected_header)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    starlette_app = Starlette(routes=[Mount(remote.path, app=mcp_asgi_app)], lifespan=lifespan)

    logger.info(
        "win32-mcp-server v%s starting HTTP transport on http://%s:%d%s (%d tools registered)",
        __version__,
        remote.host,
        remote.port,
        remote.path,
        len(registry.tool_names),
    )
    if expected_header is None:
        logger.warning(
            "Remote HTTP transport is running WITHOUT authentication. "
            "Restrict this to a trusted network only.",
        )

    uvicorn_config = uvicorn.Config(starlette_app, host=remote.host, port=remote.port, log_level="warning")
    await uvicorn.Server(uvicorn_config).serve()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows Automation Inspector MCP server")
    parser.add_argument("--version", action="store_true", help="Print server version and exit")
    parser.add_argument("--list-tools", action="store_true", help="Print registered tool names as JSON and exit")
    parser.add_argument("--health-check", action="store_true", help="Run health_check once as JSON and exit")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP instead of stdio, for remote clients such as a phone MCP app "
        "(see the WIN32_MCP_REMOTE_* environment variables)",
    )
    parser.add_argument("--host", type=str, default=None, help="Bind host for --http (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port for --http (default: 8765)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Synchronous entry point for console_scripts."""
    args = _build_arg_parser().parse_args(argv)
    if args.version:
        sys.stdout.write(f"{__version__}\n")
        return
    if args.list_tools:
        sys.stdout.write(f"{json.dumps(registry.tool_names, indent=2)}\n")
        return
    if args.health_check:
        sys.stdout.write(f"{json.dumps(asyncio.run(handle_health_check({})), indent=2, default=str)}\n")
        return

    if args.host is not None:
        config.remote.host = args.host
    if args.port is not None:
        config.remote.port = args.port
    use_http = args.http or config.remote.enabled

    try:
        asyncio.run(async_main_http() if use_http else async_main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as exc:
        logger.critical("Server crashed: %s", exc, exc_info=True)
        sys.exit(1)
