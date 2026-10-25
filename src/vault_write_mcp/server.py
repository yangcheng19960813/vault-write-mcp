"""Official MCP Streamable HTTP transport and a small operator CLI."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import ipaddress
import json
import logging
import os
import sqlite3
from typing import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.responses import JSONResponse
import uvicorn

from .store import VaultError, VaultStore, init_vault
from .config import load_settings
from .worker import CommitWorker


class BearerAuth:
    """Protect every HTTP request, without logging or persisting the credential."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = next((v for k, v in scope["headers"] if k.lower() == b"authorization"), b"")
            if not hmac.compare_digest(supplied, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _validate_token(token: str) -> None:
    if not token or not token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("VAULT_WRITE_TOKEN must be a nonempty printable ASCII token without spaces.")


def _validate_auth(enabled: bool, token: str, host: str):
    if enabled:
        _validate_token(token)
    address = ipaddress.ip_address(host)
    if not enabled and not (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")):
        raise ValueError("auth.enabled=false requires a loopback or Tailscale IPv4 bind address.")
    return address


def create_app(store: VaultStore, token: str, host: str = "127.0.0.1", port: int = 18182):
    address = _validate_auth(store.settings.auth.enabled, token, host)
    authority = f"[{address}]" if address.version == 6 else str(address)
    allowed_hosts = [f"{authority}:{port}"]
    if address.is_loopback:
        allowed_hosts.append(f"localhost:{port}")
    mcp = FastMCP(
        "VaultWrite", stateless_http=True, json_response=True,
        instructions="Read the latest version before writing. Every write and append requires expected_version, actor, and request_id. On VERSION_CONFLICT, re-read, merge and retry with a new request_id. For a transport failure, retry the identical request_id and arguments.",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=[]),
    )

    async def invoke(method: Callable, **arguments) -> CallToolResult:
        try:
            payload = {"ok": True, **await asyncio.to_thread(method, **arguments)}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))], structuredContent=payload)
        except VaultError as exc:
            payload = {"ok": False, "error": exc.as_dict()}
        except (OSError, ValueError, sqlite3.Error) as exc:
            logging.exception("Vault operation failed")
            payload = {"ok": False, "error": {"code": "STORAGE_ERROR", "message": str(exc)}}
        return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))], structuredContent=payload)

    @mcp.tool()
    async def read_note(path: str) -> CallToolResult:
        """Read a UTF-8 Markdown note and its current version; absent paths return version 'missing'."""
        return await invoke(store.read_note, path=path)

    @mcp.tool()
    async def write_note(path: str, content: str, expected_version: str, request_id: str, actor: str) -> CallToolResult:
        """Create or replace a note with a mandatory version check. Use 'missing' only to create an absent note. Keep request_id and all arguments identical when retrying a transport failure."""
        return await invoke(store.write_note, path=path, content=content, expected_version=expected_version, request_id=request_id, actor=actor)

    @mcp.tool()
    async def append_note(path: str, content: str, expected_version: str, request_id: str, actor: str) -> CallToolResult:
        """Append exact text to an existing note with a mandatory version check; no automatic newline is inserted. Re-read and merge on VERSION_CONFLICT."""
        return await invoke(store.append_note, path=path, content=content, expected_version=expected_version, request_id=request_id, actor=actor)

    @mcp.tool()
    async def note_history(path: str, limit: int = 20) -> CallToolResult:
        """List up to 100 Git commits affecting a Markdown note, newest first."""
        return await invoke(store.note_history, path=path, limit=limit)

    app = mcp.streamable_http_app()
    if store.settings.commit.mode == "interval" or store.settings.push.enabled:
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            async with original_lifespan(application) as state:
                worker = CommitWorker(store)
                worker.start()
                try:
                    yield state
                finally:
                    await asyncio.to_thread(worker.stop)

        app.router.lifespan_context = lifespan
    return BearerAuth(app, token) if store.settings.auth.enabled else app


def main() -> None:
    parser = argparse.ArgumentParser(description="VaultWrite MCP: conditional Markdown writes and Git history (Linux/macOS).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init", help="Initialize a new, empty test vault.")
    initialize.add_argument("--vault", required=True)
    serve = subparsers.add_parser("serve", help="Serve an existing Git vault over Streamable HTTP.")
    serve.add_argument("--vault", required=True)
    serve.add_argument("--host", default="127.0.0.1", help="Explicit bind IP; defaults to loopback.")
    serve.add_argument("--port", default=18182, type=int)
    serve.add_argument("--config", help="Optional TOML configuration file.")
    flush = subparsers.add_parser("flush", help="Commit pending interval writes and optionally push.")
    flush.add_argument("--vault", required=True)
    flush.add_argument("--config", help="Use the same TOML configuration as serve.")
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(init_vault(args.vault))
            return
        settings = load_settings(args.config)
        if args.command == "flush":
            store = VaultStore(args.vault, settings)
            print(json.dumps({"commit": store.flush(), "push": store.push()}, ensure_ascii=False))
            return
        token = os.environ.get("VAULT_WRITE_TOKEN", "")
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535.")
        # Validate authentication and the bind address before opening or recovering a vault.
        _validate_auth(settings.auth.enabled, token, args.host)
        app = create_app(VaultStore(args.vault, settings), token, args.host, args.port)
    except (VaultError, OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
