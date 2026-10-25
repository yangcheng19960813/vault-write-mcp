import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from vault_write_mcp.store import init_vault


@pytest.fixture
def endpoint(tmp_path, request):
    init_vault(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    token = "integration-test-token"
    log = tmp_path / ".git" / "server-test.log"
    command = [sys.executable, "-m", "vault_write_mcp.server", "serve", "--vault", str(tmp_path), "--port", str(port)]
    if getattr(request, "param", None) == "interval":
        config = tmp_path / ".git/server-test.toml"
        config.write_text('[commit]\nmode="interval"\ninterval_seconds=1\n')
        command.extend(["--config", str(config)])
    with log.open("w+") as output:
        process = subprocess.Popen(
            command,
            env={**os.environ, "VAULT_WRITE_TOKEN": token}, stdout=output, stderr=output,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(log.read_text())
                try:
                    if httpx.get(url, timeout=0.2, trust_env=False).status_code == 401:
                        break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                pytest.fail("Server did not start: " + log.read_text())
            yield url, token
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@asynccontextmanager
async def client(endpoint):
    url, token = endpoint
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False) as http, streamable_http_client(url, http_client=http) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def test_real_mcp_handshake_auth_schema_and_version_errors(endpoint):
    url, _ = endpoint
    async with httpx.AsyncClient(trust_env=False) as http:
        assert (await http.post(url, json={})).status_code == 401
        assert (await http.post(url, headers={"Authorization": "Bearer wrong"}, json={})).status_code == 401
    async with client(endpoint) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {"read_note", "write_note", "append_note", "note_history"}
        for name in ("write_note", "append_note"):
            assert {"expected_version", "request_id", "actor"} <= set(tools[name].inputSchema["required"])
        missing = await session.call_tool("write_note", {"path": "note.md", "content": "bad", "request_id": "bad", "actor": "test"})
        assert missing.isError
        absent = await session.call_tool("read_note", {"path": "note.md"})
        assert absent.structuredContent["version"] == "missing"
        arguments = {"path": "note.md", "content": "# Test\n", "expected_version": "missing", "request_id": "first", "actor": "test"}
        created = await session.call_tool("write_note", arguments)
        assert not created.isError
        assert created.structuredContent["ok"]
        replay = await session.call_tool("write_note", arguments)
        assert replay.structuredContent["replayed"]
        blind = await session.call_tool("write_note", {**arguments, "expected_version": "blind", "request_id": "blind"})
        assert blind.isError
        assert blind.structuredContent["error"]["code"] == "INVALID_VERSION"
        stale = await session.call_tool("write_note", {**arguments, "request_id": "stale"})
        assert stale.isError
        assert stale.structuredContent["error"]["code"] == "VERSION_CONFLICT"
        history = await session.call_tool("note_history", {"path": "note.md"})
        assert len(history.structuredContent["commits"]) == 1


async def test_two_independent_mcp_clients_conflict_merge_and_retry(endpoint):
    async with client(endpoint) as a, client(endpoint) as b:
        created = await a.call_tool("write_note", {"path": "note.md", "content": "# Test\n", "expected_version": "missing", "request_id": "create", "actor": "test"})
        initial = created.structuredContent["version"]
        args_a = {"path": "note.md", "content": "A\n", "expected_version": initial, "request_id": "A", "actor": "A"}
        args_b = {**args_a, "content": "B\n", "request_id": "B", "actor": "B"}
        results = await asyncio.gather(a.call_tool("append_note", args_a), b.call_tool("append_note", args_b))
        assert sum(result.isError for result in results) == 1
        loser_index = next(i for i, result in enumerate(results) if result.isError)
        assert results[loser_index].structuredContent["error"]["code"] == "VERSION_CONFLICT"
        session, arguments = [(a, args_a), (b, args_b)][loser_index]
        current = await session.call_tool("read_note", {"path": "note.md"})
        retried = await session.call_tool("append_note", {**arguments, "expected_version": current.structuredContent["version"], "request_id": arguments["request_id"] + "-retry"})
        assert not retried.isError
        final = await a.call_tool("read_note", {"path": "note.md"})
        assert final.structuredContent["content"].count("A\n") == final.structuredContent["content"].count("B\n") == 1


async def test_http_host_and_browser_origin_restrictions(endpoint):
    url, token = endpoint
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.post(url, headers={**headers, "Host": "attacker.example"}, json={})
        assert response.status_code == 421
        response = await http.post(url, headers={**headers, "Origin": "https://attacker.example"}, json={})
        assert response.status_code == 403


def test_documented_client_example(endpoint):
    url, token = endpoint
    result = subprocess.run(
        [sys.executable, "examples/smoke_client.py", "--url", url],
        env={**os.environ, "VAULT_WRITE_TOKEN": token}, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert '"conflict_rejected": true' in result.stdout
    assert '"retry_deduplicated": true' in result.stdout


@pytest.mark.parametrize("endpoint", ["interval"], indirect=True)
async def test_configured_interval_worker_commits_pending_mcp_writes(endpoint):
    async with client(endpoint) as session:
        arguments = {"path": "note.md", "content": "# Pending\n", "expected_version": "missing", "request_id": "interval-create", "actor": "test"}
        created = await session.call_tool("write_note", arguments)
        assert not created.isError
        assert created.structuredContent["pending"] and created.structuredContent["commit"] is None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            replay = await session.call_tool("write_note", arguments)
            assert not replay.isError and replay.structuredContent["replayed"]
            if not replay.structuredContent["pending"]:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("The interval worker did not commit the pending note")
        history = await session.call_tool("note_history", {"path": "note.md"})
        assert len(history.structuredContent["commits"]) == 1
        assert history.structuredContent["commits"][0]["subject"] == "docs: 更新 1 篇知识笔记"
