"""Measure compact find_note JSON and MCP HTTP response-body sizes locally.

Run with: uv run python examples/compact_find_benchmark.py

Uses a disposable synthetic Vault and an in-process ASGI HTTP transport. The
HTTP metric is the exact response-body bytes emitted by the MCP ASGI app for
each tool call (not headers/chunk framing). It is not a token measurement.
"""

import asyncio
import json
from pathlib import Path
import tempfile

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from vault_write_mcp.server import create_app
from vault_write_mcp.store import VaultStore, init_vault


class CaptureBodies:
    def __init__(self, app):
        self.app = app
        self.bodies: list[bytes] = []

    async def __call__(self, scope, receive, send):
        chunks = bytearray()

        async def captured_send(message):
            if message["type"] == "http.response.body":
                chunks.extend(message.get("body", b""))
                if not message.get("more_body", False) and scope["path"] == "/mcp" and scope["method"] == "POST":
                    self.bodies.append(bytes(chunks))
            await send(message)

        await self.app(scope, receive, captured_send)


def resolve(result, reference):
    window = result["windows"][reference["window_index"]]
    assert window["start_line"] + reference["line_index"] == reference["line"]
    return window["lines"][reference["line_index"]]


async def measure(root: Path):
    store = VaultStore(root)
    # Deterministic synthetic data intentionally makes duplicated match text
    # large enough to distinguish serialized response sizes.
    match = "MATCH " + "repeatable-data " * 70
    store.write_note("note.md", match + "\nnearby\n" + match + "\n", "missing", "seed", "benchmark")

    token = "local-compact-benchmark-token"
    app = create_app(store, token=token, host="127.0.0.1", port=18182)
    captured = CaptureBodies(app)
    # create_app wraps the Starlette app with bearer auth when authentication
    # is enabled; enter its lifespan to initialize FastMCP's session manager.
    starlette_app = getattr(app, "app", app)
    transport = httpx.ASGITransport(app=captured)
    headers = {"Authorization": f"Bearer {token}"}
    url = "http://127.0.0.1:18182/mcp"
    async with starlette_app.router.lifespan_context(starlette_app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18182",
                                     headers=headers, timeout=30, trust_env=False) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    options = {"path": "note.md", "text": "MATCH", "context_before": 1, "context_after": 1}
                    before = len(captured.bodies)
                    default = await session.call_tool("find_note", options)
                    default_body = next(body for body in captured.bodies[before:] if b"MATCH " in body)
                    before = len(captured.bodies)
                    compact = await session.call_tool("find_note", {**options, "compact": True})
                    compact_body = next(body for body in captured.bodies[before:] if b"MATCH " in body)

    default_result = default.structuredContent
    compact_result = compact.structuredContent
    assert [resolve(compact_result, ref) for ref in compact_result["matches"]] == default_result["matches"]
    default_json_bytes = len(json.dumps(default_result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    compact_json_bytes = len(json.dumps(compact_result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    print(f"serialized result JSON bytes: default={default_json_bytes}, compact={compact_json_bytes}")
    print(f"MCP HTTP response-body bytes: default={len(default_body)}, compact={len(compact_body)}")
    print("Exact match lines and anchors are semantically equal; no token estimate is made.")


def main():
    with tempfile.TemporaryDirectory(prefix="vault-write-compact-benchmark-") as directory:
        root = Path(directory)
        init_vault(root)
        asyncio.run(measure(root))


if __name__ == "__main__":
    main()
