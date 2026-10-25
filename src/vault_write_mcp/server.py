"""Official MCP Streamable HTTP transport and a small operator CLI."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import os
from typing import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.responses import JSONResponse
import uvicorn

from .store import VaultError, VaultStore, init_vault


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


def create_app(store: VaultStore, token: str, host: str = "127.0.0.1", port: int = 18182):
    _validate_token(token)
    address = ipaddress.ip_address(host)
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
        except (OSError, ValueError) as exc:
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

    return BearerAuth(mcp.streamable_http_app(), token)


def main() -> None:
    parser = argparse.ArgumentParser(description="VaultWrite MCP: conditional Markdown writes and Git history (Linux/macOS).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init", help="Initialize a new, empty test vault.")
    initialize.add_argument("--vault", required=True)
    serve = subparsers.add_parser("serve", help="Serve an existing Git vault over authenticated Streamable HTTP.")
    serve.add_argument("--vault", required=True)
    serve.add_argument("--host", default="127.0.0.1", help="Explicit bind IP; defaults to loopback.")
    serve.add_argument("--port", default=18182, type=int)
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(init_vault(args.vault))
            return
        token = os.environ.get("VAULT_WRITE_TOKEN", "")
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535.")
        # Validate the token and bind address before opening or recovering a vault.
        _validate_token(token)
        ipaddress.ip_address(args.host)
        app = create_app(VaultStore(args.vault), token, args.host, args.port)
    except (VaultError, OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
