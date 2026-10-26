from dataclasses import replace
import json
import multiprocessing
import os
import subprocess
import threading

import pytest

from vault_write_mcp.config import CommitConfig, PushConfig, Settings, SyncConfig
from vault_write_mcp.store import VaultError, VaultStore, init_vault


SYNC = Settings(push=PushConfig(enabled=True), sync=SyncConfig(enabled=True))


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@localhost", *args], text=True).strip()


@pytest.fixture
def pair(tmp_path):
    remote, main, writer = (tmp_path / name for name in ("remote.git", "main", "writer"))
    git(tmp_path, "init", "--bare", str(remote))
    init_vault(main)
    seed = VaultStore(main)
    seed.write_note("note.md", "base\n", "missing", "seed", "test")
    git(main, "remote", "add", "origin", str(remote))
    git(main, "push", "origin", "main")
    git(tmp_path, "clone", "-b", "main", str(remote), str(writer))
    git(writer, "switch", "-c", "ubuntu")
    return remote, VaultStore(main), VaultStore(writer, SYNC)


def write(store, name, content, request):
    return store.write_note(name, content, store.read_note(name)["version"], request, "test")


def publish(main):
    git(main.root, "push", "origin", "main")


def test_clean_merge_and_idle_round(pair):
    remote, main, writer = pair
    write(main, "human.md", "human", "human")
    publish(main)
    write(writer, "agent.md", "agent", "agent")
    result = writer.sync()
    assert result["state"] == "synced", result
    assert result["published"] and result["applied"]
    assert writer.read_note("human.md")["content"] == "human"
    head = writer._head()
    assert git(remote, "rev-parse", "main") == head
    assert git(writer.root, "show", "-s", "--format=%P", head).split() == [result["main_snapshot"], result["writer_snapshot"]]
    assert writer.sync()["merged_commit"] == head
    assert writer._head() == head
    assert git(writer.root, "status", "--porcelain") == ""


@pytest.mark.parametrize("again_conflict", [False, True])
def test_conflict_resolution_keeps_new_writer_history(pair, again_conflict):
    remote, main, writer = pair
    write(main, "note.md", "human\n", "human")
    publish(main)
    a = write(writer, "note.md", "agent\n", "A")["commit"]
    result = writer.sync()
    assert result["state"] == "conflict", result
    assert result["conflict_paths"] == ["note.md"]
    assert git(remote, "rev-parse", "ubuntu") == a
    assert "<<<<<<<" not in writer.read_note("note.md")["content"]
    b = write(writer, "note.md" if again_conflict else "new.md", "new agent\n", "B")["commit"]
    assert writer.sync()["state"] == "conflict"
    git(main.root, "fetch", "origin", "ubuntu")
    merge = subprocess.run(["git", "-C", str(main.root), "-c", "user.name=test", "-c", "user.email=test@localhost", "merge", "--no-commit", a], capture_output=True)
    assert merge.returncode == 1
    (main.root / "note.md").write_text("resolved human and agent\n")
    git(main.root, "add", "note.md")
    git(main.root, "commit", "-m", "Resolve A")
    publish(main)
    resolved = main._head()
    result = writer.sync()
    assert result["state"] == ("conflict" if again_conflict else "synced"), result
    git(writer.root, "merge-base", "--is-ancestor", b, "HEAD")
    if not again_conflict:
        git(writer.root, "merge-base", "--is-ancestor", resolved, "HEAD")
        assert writer.read_note("new.md")["content"] == "new agent\n"
    else:
        assert writer._head() == b
        assert git(remote, "rev-parse", "main") == resolved


@pytest.mark.parametrize("interval", [False, True])
@pytest.mark.parametrize("pause_stage", ["push", "merge-tree"])
def test_writes_continue_during_network_and_merge(pair, monkeypatch, interval, pause_stage):
    remote, main, writer = pair
    write(main, "human.md", "human", "human")
    publish(main)
    write(writer, "agent.md", "agent", "agent")
    if interval:
        writer = VaultStore(writer.root, replace(SYNC, commit=CommitConfig(mode="interval")))
    entered, release = threading.Event(), threading.Event()
    original = writer._git_run
    def paused(*args, **kwargs):
        if args[0] == pause_stage and not entered.is_set():
            entered.set()
            assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(writer, "_git_run", paused)
    results = []
    thread = threading.Thread(target=lambda: results.append(writer.sync()))
    thread.start()
    assert entered.wait(15)
    other = VaultStore(writer.root, writer.settings)
    write(other, "during.md", "during", "during")
    release.set()
    thread.join(20)
    assert not thread.is_alive()
    assert results[0]["state"] == "retry", results
    assert results[0]["published"]
    assert git(remote, "rev-parse", "main") == results[0]["merged_commit"]
    assert other.read_note("during.md")["content"] == "during"
    assert writer.sync()["state"] == "synced"
    assert writer.read_note("during.md")["content"] == "during"


def crash_sync(root, stage):
    class Crashing(VaultStore):
        def _checkpoint(self, actual):
            if actual == stage:
                os._exit(77)
    Crashing(root, SYNC).sync()


@pytest.mark.parametrize("stage", ["sync_prepared", "sync_file_written", "sync_ref_updated", "sync_index_updated"])
def test_checkout_actual_process_exit_recovers(pair, stage):
    _, main, writer = pair
    write(main, "a.md", "a", "a")
    write(main, "b.md", "b", "b")
    publish(main)
    process = multiprocessing.get_context("spawn").Process(target=crash_sync, args=(str(writer.root), stage))
    process.start()
    process.join(20)
    assert process.exitcode == 77
    if stage == "sync_file_written":
        assert (writer.root / "a.md").exists()
        assert not (writer.root / "b.md").exists()
    recovered = VaultStore(writer.root, SYNC)
    assert recovered.read_note("a.md")["content"] == "a"
    assert recovered.read_note("b.md")["content"] == "b"
    assert git(writer.root, "status", "--porcelain") == ""
    assert not recovered.journal.exists()


def test_recovery_protects_external_modification(pair):
    _, main, writer = pair
    write(main, "a.md", "a", "a")
    write(main, "b.md", "b", "b")
    publish(main)
    process = multiprocessing.get_context("spawn").Process(target=crash_sync, args=(str(writer.root), "sync_file_written"))
    process.start()
    process.join(20)
    assert process.exitcode == 77
    (writer.root / "a.md").write_text("external")
    with pytest.raises(VaultError) as caught:
        VaultStore(writer.root, SYNC)
    assert caught.value.code == "RECOVERY_CONFLICT"
    assert (writer.root / "a.md").read_text() == "external"
    assert not (writer.root / "b.md").exists()


def test_push_main_race_retries_without_loss(pair, monkeypatch):
    remote, main, writer = pair
    write(writer, "agent.md", "agent", "agent")
    original = writer._git
    advanced = []
    def race(*args, **kwargs):
        if args[0] == "push" and args[-1].endswith(":refs/heads/main") and not advanced:
            write(main, "race.md", "race", "race")
            publish(main)
            advanced.append(main._head())
        return original(*args, **kwargs)
    monkeypatch.setattr(writer, "_git", race)
    assert writer.sync()["state"] == "error"
    assert git(remote, "rev-parse", "main") == advanced[0]
    assert writer.sync()["state"] == "synced"
    assert writer.read_note("race.md")["content"] == "race"


@pytest.mark.parametrize("external", ["staged", "untracked", "dirty", "symlink"])
def test_external_changes_and_unsupported_trees_block(pair, external):
    remote, main, writer = pair
    before = git(remote, "rev-parse", "main")
    if external in ("staged", "untracked", "dirty"):
        name = "note.md" if external != "untracked" else "extra.md"
        (writer.root / name).write_text("external")
        if external == "staged":
            git(writer.root, "add", name)
    else:
        if external == "symlink":
            (main.root / "link.md").symlink_to("note.md")
        else:
            (main.root / "binary.md").write_bytes(b"\0binary")
        git(main.root, "add", ".")
        git(main.root, "commit", "-m", external)
        publish(main)
    remote_main = git(remote, "rev-parse", "main")
    result = writer.sync()
    assert result["state"] == "blocked", result
    assert git(remote, "rev-parse", "main") == remote_main
    assert writer._head() == before


def test_network_failure_redacts_status_and_keeps_writes(pair, monkeypatch):
    _, _, writer = pair
    written = write(writer, "agent.md", "agent", "agent")
    original = writer._git
    def fail(*args, **kwargs):
        if args[0] == "push":
            raise VaultError("GIT_ERROR", "https://secret:token@host/private")
        return original(*args, **kwargs)
    monkeypatch.setattr(writer, "_git", fail)
    assert writer.sync()["stage"] == "push_writer"
    assert "secret" not in (writer.state / "sync.json").read_text()
    restarted = VaultStore(writer.root, SYNC)
    assert restarted.write_note("agent.md", "agent", "missing", "agent", "test")["replayed"]
    assert restarted._head() == written["commit"]
    assert restarted.sync()["state"] == "synced"


def test_configuration_and_status(pair):
    _, main, writer = pair
    with pytest.raises(ValueError):
        Settings(sync=SyncConfig(enabled=True))
    with pytest.raises(VaultError):
        VaultStore(main.root, SYNC)
    with pytest.raises(VaultError):
        VaultStore(writer.root, replace(SYNC, push=PushConfig(enabled=True, branch="main")))
    assert writer.sync_status()["state"] == "idle"
    assert writer.sync_status()["pending_count"] == 0
    assert main.sync()["state"] == "disabled"


def test_hidden_configuration_binary_and_deletion_rename(pair):
    _, main, writer = pair
    (main.root / ".obsidian").mkdir()
    (main.root / ".obsidian/config.json").write_text("{}")
    (main.root / "image.bin").write_bytes(b"\0\xff" * 600000)
    git(main.root, "mv", "note.md", "renamed.md")
    git(main.root, "add", ".")
    git(main.root, "commit", "-m", "Configuration attachment and rename")
    publish(main)
    assert writer.sync()["state"] == "synced"
    assert (writer.root / "image.bin").read_bytes() == b"\0\xff" * 600000
    assert (writer.root / ".obsidian/config.json").read_text() == "{}"
    assert not (writer.root / "note.md").exists()
    assert writer.read_note("renamed.md")["content"] == "base\n"


def test_checkout_preserves_nonstandard_permissions_and_remains_writable(pair):
    _, main, writer = pair
    (writer.root / "note.md").chmod(0o640)
    write(main, "note.md", "human", "human")
    publish(main)
    assert writer.sync()["state"] == "synced"
    assert (writer.root / "note.md").stat().st_mode & 0o777 == 0o640
    assert not writer.journal.exists()
    assert write(writer, "after.md", "after", "after")["changed"]


def test_interval_write_between_flush_and_snapshot_still_publishes_snapshot(pair, monkeypatch):
    remote, main, writer = pair
    write(main, "human.md", "human", "human")
    publish(main)
    writer = VaultStore(writer.root, replace(SYNC, commit=CommitConfig(mode="interval")))
    write(writer, "agent.md", "agent", "agent")
    original = writer.flush
    def flush_then_write():
        result = original()
        write(writer, "later.md", "later", "later")
        return result
    monkeypatch.setattr(writer, "flush", flush_then_write)
    result = writer.sync()
    assert result["published"] and result["state"] == "retry"
    assert git(remote, "rev-parse", "main") == result["merged_commit"]
    assert writer.read_note("later.md")["content"] == "later"



def test_status_distinguishes_new_writes_and_changed_target(pair):
    _, _, writer = pair
    assert writer.sync()["state"] == "synced"
    assert writer.sync_status()["current"]
    write(writer, "new.md", "new", "new")
    status = writer.sync_status()
    assert status["last_state"] == "synced" and status["state"] == "pending" and not status["current"]
    other = VaultStore(writer.root, replace(SYNC, sync=SyncConfig(enabled=True, main_branch="other")))
    assert other.sync_status()["state"] == "idle"
    assert not other.sync_status()["current"]


def test_custom_merge_driver_and_hooks_never_execute(pair):
    _, main, writer = pair
    marker = writer.state / "driver-ran"
    script = writer.state / "driver.sh"
    script.write_text("#!/bin/sh\ntouch " + str(marker) + "\nexit 0\n")
    script.chmod(0o755)
    git(writer.root, "config", "merge.evil.driver", str(script))
    git(writer.root, "config", "core.hooksPath", str(writer.state))
    hook = writer.state / "pre-push"
    hook.write_text(script.read_text())
    hook.chmod(0o755)
    (writer.root / ".gitattributes").write_text("note.md merge=evil\n")
    git(writer.root, "add", ".gitattributes")
    git(writer.root, "-c", "core.hooksPath=/dev/null", "commit", "-m", "Attributes")
    write(writer, "note.md", "agent\n", "agent")
    write(main, "note.md", "human\n", "human")
    publish(main)
    assert writer.sync()["state"] == "conflict"
    assert not marker.exists()


def parallel_sync(root, entered, release, queue, first):
    class Paused(VaultStore):
        def _git_run(self, *args, **kwargs):
            if args[0] == "push" and first and not entered.is_set():
                entered.set()
                assert release.wait(20)
            return super()._git_run(*args, **kwargs)
    result = Paused(root, SYNC).sync()
    queue.put(result)


def test_cross_process_sync_serialization(pair):
    _, main, writer = pair
    write(main, "human.md", "human", "human")
    publish(main)
    write(writer, "agent.md", "agent", "agent")
    context = multiprocessing.get_context("spawn")
    entered, release, queue = context.Event(), context.Event(), context.Queue()
    first = context.Process(target=parallel_sync, args=(str(writer.root), entered, release, queue, True))
    second = context.Process(target=parallel_sync, args=(str(writer.root), entered, release, queue, False))
    first.start()
    assert entered.wait(15)
    second.start()
    write(writer, "during.md", "during", "during")
    release.set()
    results = [queue.get(timeout=30), queue.get(timeout=30)]
    for process in (first, second):
        process.join(20)
        assert process.exitcode == 0
    assert sorted(result["state"] for result in results) == ["retry", "synced"]
    assert writer.read_note("during.md")["content"] == "during"
    git(writer.root, "fsck", "--no-dangling")


def test_cli_and_worker_share_sync_path(pair):
    import sys
    from vault_write_mcp.worker import CommitWorker
    _, main, writer = pair
    write(main, "human.md", "human", "human")
    publish(main)
    assert CommitWorker(writer).tick()["state"] == "synced"
    config = writer.state / "cli.toml"
    config.write_text('[push]\nenabled=true\n[sync]\nenabled=true\n')
    for command in ("sync", "sync-status"):
        result = subprocess.run([sys.executable, "-m", "vault_write_mcp.server", command, "--vault", str(writer.root), "--config", str(config)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["state"] == "synced"



def test_ignored_parent_obstruction_does_not_create_journal(pair):
    _, main, writer = pair
    (writer.root / ".gitignore").write_text("/notes\n")
    git(writer.root, "add", ".gitignore")
    git(writer.root, "commit", "-m", "Ignore notes")
    (writer.root / "notes").write_text("external ignored file")
    write(main, "notes/new.md", "new", "new")
    publish(main)
    result = writer.sync()
    assert result["state"] == "blocked", result
    assert not writer.journal.exists()
    assert (writer.root / "notes").read_text() == "external ignored file"
    assert write(writer, "unrelated.md", "unrelated", "unrelated")["changed"]


@pytest.mark.parametrize("names", [("A.md", "a.md"), ("é.md", "é.md")])
def test_incoming_case_and_unicode_collisions_block_before_journal(pair, names):
    _, main, writer = pair
    blob = main._git("hash-object", "-w", "--stdin", data=b"content").decode().strip()
    tree = main._git("mktree", data="".join(f"100644 blob {blob}\t{name}\n" for name in names).encode()).decode().strip()
    commit = main._git("commit-tree", tree, "-p", main._head(), data=b"Incoming colliding paths\n").decode().strip()
    main._git("update-ref", main._ref(), commit, main._head())
    publish(main)
    result = writer.sync()
    assert result["state"] == "blocked" and result["stage"] == "preflight", result
    assert not writer.journal.exists()
    assert write(writer, "unrelated.md", "unrelated", "unrelated")["changed"]


@pytest.mark.parametrize("kind", ["delete_modify", "binary", "topology"])
def test_nontrivial_conflicts_or_topology_preserve_writer(pair, kind):
    remote, main, writer = pair
    if kind == "delete_modify":
        git(main.root, "rm", "note.md")
        write(writer, "note.md", "agent", "agent")
    elif kind == "binary":
        (main.root / "note.md").write_bytes(b"human\0binary")
        write(writer, "note.md", "agent\0binary", "agent")
    else:
        git(main.root, "rm", "note.md")
        (main.root / "note.md").mkdir()
        (main.root / "note.md/new.md").write_text("nested")
    git(main.root, "add", ".")
    git(main.root, "commit", "-m", kind)
    publish(main)
    remote_head, writer_head = main._head(), writer._head()
    result = writer.sync()
    assert result["state"] == ("blocked" if kind == "topology" else "conflict"), result
    assert not writer.journal.exists()
    assert writer._head() == writer_head
    assert git(remote, "rev-parse", "main") == remote_head
    assert write(writer, "unrelated.md", "unrelated", "unrelated")["changed"]


def test_repository_union_attribute_cannot_silently_resolve_conflict(pair):
    _, main, writer = pair
    (writer.root / ".gitattributes").write_text("note.md merge=union\n")
    git(writer.root, "add", ".gitattributes")
    git(writer.root, "commit", "-m", "Union attribute")
    write(writer, "note.md", "agent\n", "agent")
    write(main, "note.md", "human\n", "human")
    publish(main)
    assert writer.sync()["state"] == "conflict"


def crash_with_nonportable_exec_bits(root):
    class SimulatedNTFS(VaultStore):
        def _atomic_write(self, path, data, mode=0o600):
            super()._atomic_write(path, data, mode)
            if path.name in ("a.md", "b.md"):
                path.chmod(mode | 0o111)
        def _checkpoint(self, stage):
            if stage == "sync_file_written":
                os._exit(77)
    SimulatedNTFS(root, SYNC).sync()


def test_recovery_ignores_nonportable_execution_bits_with_core_filemode_false(pair):
    _, main, writer = pair
    git(writer.root, "config", "core.filemode", "false")
    write(main, "a.md", "a", "a")
    write(main, "b.md", "b", "b")
    publish(main)
    process = multiprocessing.get_context("spawn").Process(target=crash_with_nonportable_exec_bits, args=(str(writer.root),))
    process.start()
    process.join(20)
    assert process.exitcode == 77
    assert (writer.root / "a.md").stat().st_mode & 0o111 == 0o111
    recovered = VaultStore(writer.root, SYNC)
    assert recovered.read_note("a.md")["content"] == "a"
    assert recovered.read_note("b.md")["content"] == "b"
    assert not recovered.journal.exists()
    assert write(recovered, "after.md", "after", "after")["changed"]


def test_private_unknown_temp_does_not_block_sync_or_change_saved_patch_context(pair):
    remote, main, writer = pair
    unknown = writer.state / "atomic-unregistered.tmp"
    unknown.write_bytes(b"ownership cannot be proved")
    snapshot = writer.read_note_range("note.md")
    arguments = {
        "path": "note.md", "edits": [{"op": "SWAP", "anchor": snapshot["lines"][0].split("|", 1)[0], "lines": ["original result"]}],
        "expected_version": snapshot["version"], "request_id": "context", "actor": "test", "return_context": True,
    }
    first = writer.patch_note(**arguments)
    write(main, "human.md", "human\n", "human")
    publish(main)
    assert writer.sync()["state"] == "synced"
    write(writer, "note.md", "future content\n", "future")
    assert writer.sync()["state"] == "synced"
    replay = VaultStore(writer.root, SYNC).patch_note(**arguments)
    assert replay["replayed"] and replay["result_context"] == first["result_context"] and replay["version"] == first["version"]
    assert unknown.read_bytes() == b"ownership cannot be proved"
    assert "atomic-" not in git(remote, "ls-tree", "-r", "--name-only", "main")
