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
    mode = getattr(request, "param", None)
    token = None if mode in ("no-auth", "interval-no-auth") else "integration-test-token"
    log = tmp_path / ".git" / "server-test.log"
    command = [sys.executable, "-m", "vault_write_mcp.server", "serve", "--vault", str(tmp_path), "--port", str(port)]
    if mode in ("interval", "no-auth", "interval-no-auth", "sync", "policy"):
        config = tmp_path / ".git/server-test.toml"
        text = '[auth]\nenabled=false\n' if token is None else ""
        if mode in ("interval", "interval-no-auth"):
            text += '[commit]\nmode="interval"\ninterval_seconds=1\n'
        if mode == "policy":
            text += '[write_policy]\ndeny_paths=["protected"]\n'
        if mode == "sync":
            bare = tmp_path / ".git/http-remote.git"
            for args in (("init", "--bare", str(bare)), ("remote", "add", "origin", str(bare)), ("push", "origin", "main"), ("branch", "-m", "ubuntu")):
                subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
            text += '[commit]\ninterval_seconds=1\n[push]\nenabled=true\n[sync]\nenabled=true\n'
        config.write_text(text)
        command.extend(["--config", str(config)])
    env = dict(os.environ)
    if token is None:
        env.pop("VAULT_WRITE_TOKEN", None)
    else:
        env["VAULT_WRITE_TOKEN"] = token
    with log.open("w+") as output:
        process = subprocess.Popen(
            command,
            env=env, stdout=output, stderr=output,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(log.read_text())
                try:
                    status = httpx.get(url, timeout=0.2, trust_env=False).status_code
                    if status == 401 if token is not None else status in (405, 406):
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
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as http, streamable_http_client(url, http_client=http) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@pytest.mark.parametrize("endpoint", [None, "no-auth"], indirect=True)
async def test_real_mcp_handshake_auth_schema_and_version_errors(endpoint):
    url, token = endpoint
    if token is not None:
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.post(url, json={})).status_code == 401
            assert (await http.post(url, headers={"Authorization": "Bearer wrong"}, json={})).status_code == 401
    async with client(endpoint) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {"read_note", "read_note_range", "find_note", "write_note", "append_note", "patch_note", "preview_move_note", "move_note", "note_history", "sync_status"}
        for name in ("write_note", "append_note", "patch_note"):
            assert {"expected_version", "request_id", "actor"} <= set(tools[name].inputSchema["required"])
        status = await session.call_tool("sync_status", {})
        assert not status.isError and not status.structuredContent["enabled"]
        assert status.structuredContent["pending_count"] == 0
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


@pytest.mark.parametrize("endpoint", [None, "no-auth", "interval"], indirect=True)
async def test_bounded_patch_tools_work_over_real_mcp_and_reject_conflicts(endpoint):
    async with client(endpoint) as session:
        created = await session.call_tool("write_note", {"path": "note.md", "content": "# Header\nline to find\nkeep\n", "expected_version": "missing", "request_id": "create", "actor": "test"})
        found = (await session.call_tool("find_note", {"path": "note.md", "text": "line to find"})).structuredContent
        assert len(found["matches"]) == 1 and "content" not in found
        current = (await session.call_tool("read_note_range", {"path": "note.md", "start_line": 2, "max_lines": 1})).structuredContent
        assert len(current["lines"]) == 1 and current["next_line"] == 3
        assert current["version"] == created.structuredContent["version"]
        args = {"path": "note.md", "edits": [{"op": "SWAP", "anchor": found["matches"][0].split("|", 1)[0], "lines": ["replaced"]}], "expected_version": current["version"], "request_id": "patch", "actor": "test"}
        result = await session.call_tool("patch_note", args)
        assert not result.isError and result.structuredContent["changed"]
        assert (await session.call_tool("patch_note", args)).structuredContent["replayed"]
        stale = await session.call_tool("patch_note", {**args, "request_id": "stale"})
        assert stale.isError and stale.structuredContent["error"]["code"] == "VERSION_CONFLICT"
        final = (await session.call_tool("read_note", {"path": "note.md"})).structuredContent
        assert final["content"] == "# Header\nreplaced\nkeep\n"
        invalid = await session.call_tool("patch_note", {**args, "expected_version": final["version"], "request_id": "typo", "edits": [{**args["edits"][0], "unknown_field": "typo"}]})
        assert invalid.isError
        assert (await session.call_tool("read_note", {"path": "note.md"})).structuredContent["version"] == final["version"]


@pytest.mark.parametrize("endpoint", [None, "no-auth"], indirect=True)
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


@pytest.mark.parametrize("endpoint", [None, "no-auth"], indirect=True)
async def test_http_host_and_browser_origin_restrictions(endpoint):
    url, token = endpoint
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
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


@pytest.mark.parametrize("endpoint", ["interval", "interval-no-auth"], indirect=True)
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

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "192.168.1.1"])
def test_no_auth_rejects_other_bind_addresses_before_opening_vault(tmp_path, host):
    config = tmp_path / "server.toml"
    config.write_text("[auth]\nenabled=false\n")
    vault = tmp_path / "not-a-vault"
    result = subprocess.run(
        [sys.executable, "-m", "vault_write_mcp.server", "serve", "--vault", str(vault),
         "--host", host, "--config", str(config)],
        env={key: value for key, value in os.environ.items() if key != "VAULT_WRITE_TOKEN"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "loopback or Tailscale IPv4" in result.stderr
    assert not vault.exists()


@pytest.mark.parametrize("endpoint", ["sync"], indirect=True)
async def test_sync_worker_and_status_over_real_mcp(endpoint):
    async with client(endpoint) as session:
        created = await session.call_tool("write_note", {"path": "note.md", "content": "saved", "expected_version": "missing", "request_id": "sync-create", "actor": "test"})
        assert not created.isError
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = (await session.call_tool("sync_status", {})).structuredContent
            assert state["enabled"] and state["writer_commit"] == created.structuredContent["commit"]
            if state["current"]:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("Sync worker did not publish the note: " + str(state))
        assert state["published"] and state["applied"] and state["state"] == "synced"


@pytest.mark.parametrize("endpoint", [None, "no-auth", "interval", "sync"], indirect=True)
async def test_context_schema_new_anchors_and_snapshot_replay_over_real_mcp(endpoint):
    async with client(endpoint) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        for name in ("find_note", "patch_note"):
            properties = tools[name].inputSchema["properties"]
            assert properties["context_before"]["default"] == 0
            assert properties["context_before"]["minimum"] == 0 and properties["context_before"]["maximum"] == 50
        compact_schema = tools["find_note"].inputSchema["properties"]["compact"]
        assert compact_schema["type"] == "boolean" and compact_schema["default"] is False
        assert tools["patch_note"].inputSchema["properties"]["return_context"]["default"] is False
        created = (await session.call_tool("write_note", {
            "path": "note.md", "content": "one\nTARGET\nthree\nfour\n", "expected_version": "missing", "request_id": "seed", "actor": "test",
        })).structuredContent
        found = (await session.call_tool("find_note", {
            "path": "note.md", "text": "TARGET", "context_before": 1, "context_after": 1,
        })).structuredContent
        assert found["version"] == created["version"] and not found["context_truncated"]
        assert len(found["windows"]) == 1 and len(found["windows"][0]["lines"]) == 3
        compact = (await session.call_tool("find_note", {
            "path": "note.md", "text": "TARGET", "compact": True,
        })).structuredContent
        assert compact["matches"] == [{"line": 2, "window_index": 0, "line_index": 0}]
        assert compact["windows"][0]["lines"] == found["matches"]
        for bad_compact in (1, 0, "true", None):
            invalid_compact = await session.call_tool("find_note", {
                "path": "note.md", "text": "TARGET", "compact": bad_compact,
            })
            assert invalid_compact.isError
        arguments = {
            "path": "note.md", "edits": [{"op": "SWAP", "anchor": found["matches"][0].split("|", 1)[0], "lines": ["NEW", "inserted"]}],
            "expected_version": found["version"], "request_id": "context-patch", "actor": "test", "return_context": True, "context_after": 1,
        }
        first = (await session.call_tool("patch_note", arguments)).structuredContent
        assert first["ok"] and not first["result_context"]["truncated"]
        fresh = first["result_context"]["windows"][0]["lines"][-1]
        assert fresh.startswith("4:") and fresh.endswith("|three")
        second = await session.call_tool("patch_note", {
            "path": "note.md", "edits": [{"op": "SWAP", "anchor": fresh.split("|", 1)[0], "lines": ["FUTURE"]}],
            "expected_version": first["version"], "request_id": "future", "actor": "test",
        })
        assert not second.isError
        replay = (await session.call_tool("patch_note", arguments)).structuredContent
        assert replay["replayed"] and replay["result_context"] == first["result_context"] and replay["version"] == first["version"]
        mismatch = await session.call_tool("patch_note", {**arguments, "context_after": 2})
        assert mismatch.isError and mismatch.structuredContent["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        for bad in (True, -1, 51, "1"):
            invalid = await session.call_tool("find_note", {"path": "note.md", "text": "NEW", "context_before": bad})
            assert invalid.isError
        invalid = await session.call_tool("patch_note", {**arguments, "return_context": "true"})
        assert invalid.isError


@pytest.mark.parametrize("endpoint", [None, "no-auth", "interval", "sync"], indirect=True)
async def test_preview_move_and_reference_updates_over_real_mcp(endpoint):
    async with client(endpoint) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools["move_note"].inputSchema["required"]) == {"source", "destination", "expected_versions", "plan_version", "request_id", "actor"}
        for path, content, request in (("old.md", "source\n", "source"), ("ref.md", "[[old#h|Alias]]\n", "ref")):
            result = await session.call_tool("write_note", {"path": path, "content": content, "expected_version": "missing", "request_id": request, "actor": "test"})
            assert not result.isError
        deadline = time.monotonic() + 8
        while True:
            preview = await session.call_tool("preview_move_note", {"source": "old.md", "destination": "archive/new.md"})
            if not preview.isError:
                break
            assert preview.structuredContent["error"]["code"] == "PENDING_BATCH"
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        plan = preview.structuredContent
        assert plan["rewritten_links"] == 1
        arguments = {key: plan[key] for key in ("source", "destination", "expected_versions", "plan_version")}
        arguments.update({"request_id": "move", "actor": "test"})
        moved = await session.call_tool("move_note", arguments)
        assert not moved.isError
        receipt = moved.structuredContent
        assert receipt["versions"]["old.md"] == "missing"
        assert (await session.call_tool("read_note", {"path": "ref.md"})).structuredContent["content"] == "[[./archive/new.md#h|Alias]]\n"
        later = await session.call_tool("write_note", {"path": "archive/new.md", "content": "future\n", "expected_version": receipt["versions"]["archive/new.md"], "request_id": "future", "actor": "test"})
        assert not later.isError
        replay = (await session.call_tool("move_note", arguments)).structuredContent
        assert replay == {**receipt, "replayed": True}
        conflict = await session.call_tool("move_note", {**arguments, "actor": "different"})
        assert conflict.isError and conflict.structuredContent["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("endpoint", ["policy"], indirect=True)
async def test_http_policy_errors_and_history_opt_in_schema(endpoint):
    async with client(endpoint) as session:
        denied = await session.call_tool("write_note", {"path": "protected/note.md", "content": "no", "expected_version": "missing", "request_id": "denied", "actor": "test"})
        assert denied.isError and not denied.structuredContent["ok"]
        assert denied.structuredContent["error"]["code"] == "PATH_PROTECTED"
        read = await session.call_tool("read_note", {"path": "protected/note.md"})
        assert read.structuredContent["version"] == "missing"
        history = await session.call_tool("note_history", {"path": "note.md", "follow_renames": True})
        assert history.structuredContent["ok"] and history.structuredContent["rename_following"] == "git_similarity_heuristic"
        invalid = await session.call_tool("note_history", {"path": "note.md", "follow_renames": "true"})
        assert invalid.isError
