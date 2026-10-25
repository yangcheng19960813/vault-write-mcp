"""Run against a temporary VaultWrite server, never against a production vault."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
import uuid

from mcp import ClientSession
import httpx
from mcp.client.streamable_http import streamable_http_client


@asynccontextmanager
async def connect(url, token):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False) as http, streamable_http_client(url, http_client=http) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def run(url, token):
    path = f"smoke/{uuid.uuid4()}.md"
    async with connect(url, token) as a, connect(url, token) as b:
        created = await a.call_tool("write_note", {"path": path, "content": "# Smoke test\n", "expected_version": "missing", "request_id": str(uuid.uuid4()), "actor": "smoke"})
        assert not created.isError, created
        version = created.structuredContent["version"]
        arguments = [{"path": path, "content": actor + "\n", "expected_version": version, "request_id": str(uuid.uuid4()), "actor": actor} for actor in ("A", "B")]
        sessions = [a, b]
        results = await asyncio.gather(*(session.call_tool("append_note", args) for session, args in zip(sessions, arguments)))
        assert sum(result.isError for result in results) == 1, results
        loser = next(i for i, result in enumerate(results) if result.isError)
        assert results[loser].structuredContent["error"]["code"] == "VERSION_CONFLICT", results
        latest = await sessions[loser].call_tool("read_note", {"path": path})
        retry_args = {**arguments[loser], "expected_version": latest.structuredContent["version"], "request_id": str(uuid.uuid4())}
        retried = await sessions[loser].call_tool("append_note", retry_args)
        assert not retried.isError, retried
        repeated = await sessions[loser].call_tool("append_note", retry_args)
        assert not repeated.isError and repeated.structuredContent["replayed"], repeated
        final = await a.call_tool("read_note", {"path": path})
        text = final.structuredContent["content"]
        assert text.count("A\n") == text.count("B\n") == 1, text
        history = await a.call_tool("note_history", {"path": path})
        assert len(history.structuredContent["commits"]) == 3, history
        print(json.dumps({"ok": True, "path": path, "content": text, "commits": 3, "conflict_rejected": True, "retry_deduplicated": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18182/mcp")
    args = parser.parse_args()
    asyncio.run(run(args.url, os.environ["VAULT_WRITE_TOKEN"]))
