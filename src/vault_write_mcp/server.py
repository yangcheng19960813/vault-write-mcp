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
from typing import Annotated, Callable

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.responses import JSONResponse
import uvicorn

from .store import VaultError, VaultStore, init_vault
from .config import load_settings
from .worker import CommitWorker
from .editing import LineEdit, MAX_CONTEXT_DISTANCE


ContextDistance = Annotated[int, Field(strict=True, ge=0, le=MAX_CONTEXT_DISTANCE)]
ReturnContext = Annotated[bool, Field(strict=True)]


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
        instructions="Prefer find_note and read_note_range for bounded line reads, then patch_note for local edits. Copy line:hash anchors exactly. Every write, append and patch requires expected_version, actor, and request_id. On VERSION_CONFLICT or ANCHOR_CONFLICT, re-read the affected range, merge and retry with a new request_id. For a transport failure, retry the identical request_id and arguments.",
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
    async def read_note_range(path: str, start_line: int = 1, max_lines: int = 80) -> CallToolResult:
        """Read up to 200 lines as line:hash|text, with the whole-note version. Output is capped at 16 KiB of rendered lines. Continue at next_line when set. Missing/empty notes have no anchors; create them with write_note."""
        return await invoke(store.read_note_range, path=path, start_line=start_line, max_lines=max_lines)

    @mcp.tool()
    async def find_note(path: str, text: str, start_line: int = 1, max_matches: int = 10,
                        context_before: ContextDistance = 0, context_after: ContextDistance = 0) -> CallToolResult:
        """Find literal, case-sensitive text in one note; return matching line:hash|text and the whole-note version. Optional before/after (0–50) add merged context windows, bounded to 200 window lines and 16 KiB of rendered lines including matches. context_truncated marks omitted context. Continue matching at next_line; context does not advance it. No regex/model calls."""
        return await invoke(store.find_note, path=path, text=text, start_line=start_line, max_matches=max_matches,
                            context_before=context_before, context_after=context_after)

    @mcp.tool()
    async def write_note(path: str, content: str, expected_version: str, request_id: str, actor: str) -> CallToolResult:
        """Create or replace a note with a mandatory version check. Use 'missing' only to create an absent note. Keep request_id and all arguments identical when retrying a transport failure."""
        return await invoke(store.write_note, path=path, content=content, expected_version=expected_version, request_id=request_id, actor=actor)

    @mcp.tool()
    async def append_note(path: str, content: str, expected_version: str, request_id: str, actor: str) -> CallToolResult:
        """Append exact text to an existing note with a mandatory version check; no automatic newline is inserted. Re-read and merge on VERSION_CONFLICT."""
        return await invoke(store.append_note, path=path, content=content, expected_version=expected_version, request_id=request_id, actor=actor)

    @mcp.tool()
    async def patch_note(path: str, edits: list[LineEdit], expected_version: str, request_id: str, actor: str,
                         return_context: ReturnContext = False, context_before: ContextDistance = 0,
                         context_after: ContextDistance = 0) -> CallToolResult:
        """Apply 1–100 edits atomically against the exact version. SWAP/DEL accept an optional inclusive end_anchor; INS.PRE/INS.POST insert around one anchor. lines contains newline-free strings (DEL omits it). All anchors refer to the original snapshot; overlapping ranges/anchors are rejected. Preserve BOM and untouched bytes; SWAP and tail insertion retain EOF newline. return_context=true adds bounded result_context windows with fresh post-edit anchors; optional before/after are 0–50. At most 200 context lines/16 KiB, with truncated=true when omitted. Retries replay this original snapshot, not future content. Re-read on conflicts; retry transport failures with identical arguments, including result options."""
        return await invoke(store.patch_note, path=path, edits=[edit.model_dump(exclude_none=True) for edit in edits], expected_version=expected_version, request_id=request_id, actor=actor,
                            return_context=return_context, context_before=context_before, context_after=context_after)

    @mcp.tool()
    async def preview_move_note(source: str, destination: str) -> CallToolResult:
        """Preview a tracked Markdown move/rename and supported local reference updates without changing files. Returns plan_version and the exact expected_versions mapping required by move_note. Requires a clean vault and no pending interval batch. Rejects ambiguous/unsupported affected links; scans bounded tracked notes and assets."""
        return await invoke(store.preview_move_note, source=source, destination=destination)

    @mcp.tool()
    async def move_note(source: str, destination: str, expected_versions: dict[str, str], plan_version: str,
                        request_id: str, actor: str) -> CallToolResult:
        """Apply a previewed move/rename plus supported inbound/outgoing reference updates in one recoverable transaction and one immediate Git commit. Copy preview plan_version and complete expected_versions exactly. No implicit interval flush. Re-preview conflicts; transport retries must retain all original arguments/request_id and replay the original durable receipt. Atomic through service locking/recovery, not simultaneous multi-file visibility to external readers."""
        return await invoke(store.move_note, source=source, destination=destination, expected_versions=expected_versions,
                            plan_version=plan_version, request_id=request_id, actor=actor)

    @mcp.tool()
    async def note_history(path: str, limit: int = 20) -> CallToolResult:
        """List up to 100 Git commits affecting a Markdown note, newest first."""
        return await invoke(store.note_history, path=path, limit=limit)

    @mcp.tool()
    async def sync_status() -> CallToolResult:
        """Read sync state without network calls. Writes are saved locally and may not be published to main. last_state describes the last attempt; state=pending/current=false indicates newer writes. pending_count and writer_commit report local state; target and completed_at identify the last attempt."""
        return await invoke(store.sync_status)

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
    for name, help_text in (("sync", "Save pending writes and synchronize the writer branch with main."),
                            ("sync-status", "Read synchronization state without fetching or pushing.")):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--vault", required=True)
        command.add_argument("--config", help="Use the same TOML configuration as serve.")
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(init_vault(args.vault))
            return
        settings = load_settings(args.config)
        if args.command in ("sync", "sync-status"):
            store = VaultStore(args.vault, settings)
            result = store.sync() if args.command == "sync" else store.sync_status()
            print(json.dumps(result, ensure_ascii=False))
            return
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
