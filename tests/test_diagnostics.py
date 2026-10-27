from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from vault_write_mcp.config import Settings, WritePolicyConfig
from vault_write_mcp.diagnostics import doctor
from vault_write_mcp.store import VaultError, VaultStore, init_vault


def snapshot(root):
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
    return result


@pytest.fixture
def root(tmp_path):
    init_vault(tmp_path)
    return tmp_path


def assert_read_only(root, callback=lambda root: doctor(root)):
    before = snapshot(root)
    result = callback(root)
    assert snapshot(root) == before
    assert len(json.dumps(result).encode()) < 8192
    return result


def test_doctor_does_not_initialize_state_or_construct_store(root, monkeypatch):
    monkeypatch.setattr(VaultStore, "__init__", lambda *args, **kwargs: pytest.fail("doctor constructed a store"))
    monkeypatch.setattr(VaultStore, "_recover", lambda *args: pytest.fail("doctor recovered"))
    result = assert_read_only(root)
    assert result["read_only"] and result["journal"]["status"] == "absent"
    assert not (root / ".git/vault-write").exists()


def test_doctor_existing_store_has_read_only_advisory_counts(root):
    VaultStore(root).write_note("note.md", "private body", "missing", "create", "test")
    result = assert_read_only(root)
    assert result["receipts"]["counts"]["receipts"] == 1
    assert result["receipts"]["snapshot"] == "immutable_main_database_only"
    assert "private body" not in json.dumps(result)


def crash(root, stage):
    class Crash(VaultStore):
        def _checkpoint(self, actual):
            if actual == stage:
                os._exit(77)
    Crash(root).write_note("note.md", "sensitive body", "missing", "crash", "test")


@pytest.mark.parametrize("stage", ["prepared", "file_written", "ref_updated", "receipt_saved"])
def test_doctor_inspects_actual_exit_without_recovery_or_cleanup(root, stage):
    process = multiprocessing.get_context("spawn").Process(target=crash, args=(str(root), stage))
    process.start(); process.join(20)
    assert process.exitcode == 77
    # Add an owned stale temp: doctor must not invoke store atomic cleanup.
    state = root / ".git/vault-write"
    temporary = state / "atomic-test.tmp"
    temporary.write_bytes(b"private-temp")
    info = temporary.stat()
    with sqlite3.connect(state / "receipts.sqlite3") as db:
        db.execute("INSERT INTO atomic_files VALUES (?, ?, ?)", (temporary.name, info.st_dev, info.st_ino))
    result = assert_read_only(root)
    assert result["journal"]["status"] == "pending"
    assert result["recovery"]["state"] == "pending_checks_required"
    protected = assert_read_only(root, lambda root: doctor(root, Settings(write_policy=WritePolicyConfig(deny_paths=["note.md"]))))
    assert protected["recovery"]["state"] == "policy_blocked"
    assert "sensitive" not in json.dumps(result)
    # Unmodified original state remains recoverable by the normal store.
    assert VaultStore(root).read_note("note.md")["content"] == "sensitive body"


@pytest.mark.parametrize("payload", [b"not json private-secret", b"[]", b'"private-secret"', b'{"kind":["private-secret"]}',
                                     b'{"kind":"private-secret"}', b'{"kind":"move_note","entries":null}',
                                     b'{"path":"private-secret","old_head":3}', b"[" * 2000 + b"]" * 2000])
def test_doctor_malformed_journal_is_bounded_sanitized_and_unchanged(root, payload):
    store = VaultStore(root)
    store.journal.write_bytes(payload)
    result = assert_read_only(root)
    assert result["journal"]["status"] == "invalid_or_unsupported"
    assert "private-secret" not in json.dumps(result)


def test_doctor_journal_and_database_symlinks_are_not_followed(root, tmp_path):
    store = VaultStore(root)
    outside = root / "private-secret"
    outside.write_text("credential-secret")
    store.journal.symlink_to(outside)
    store.database.unlink(); store.database.symlink_to(outside)
    result = assert_read_only(root)
    assert result["journal"]["status"] == result["receipts"]["status"] == "unsafe_or_unreadable"
    assert "secret" not in json.dumps(result)


def test_doctor_fifo_oversize_corrupt_database_and_state_symlink(root):
    store = VaultStore(root)
    os.mkfifo(store.journal)
    assert assert_read_only(root)["journal"]["status"] == "unsafe_or_unreadable"
    store.journal.unlink()
    with store.journal.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    # Don't snapshot sparse 64 MiB bytes: verify inode/stat and DB separately.
    before = store.journal.stat()
    result = doctor(root)
    assert result["journal"]["status"] == "too_large" and store.journal.stat() == before
    store.journal.unlink()
    store.database.write_bytes(b"private-secret invalid SQLite")
    result = assert_read_only(root)
    assert result["receipts"]["status"] == "invalid_or_unsupported" and "private-secret" not in json.dumps(result)
    moved = root / ".git/other-state"
    store.state.rename(moved); store.state.symlink_to(moved)
    assert assert_read_only(root)["journal"]["status"] == "unsafe_or_unreadable"


def test_doctor_wal_is_explicitly_incomplete_and_never_creates_shm(root):
    store = VaultStore(root)
    (store.state / "receipts.sqlite3-wal").write_bytes(b"malformed private-secret")
    result = assert_read_only(root)
    assert result["receipts"]["wal_present"]
    assert not (store.state / "receipts.sqlite3-shm").exists()


def test_cli_doctor_needs_no_token_and_never_creates_store_state(root):
    env = {key: value for key, value in os.environ.items() if key != "VAULT_WRITE_TOKEN"}
    result = assert_read_only(root, lambda root: subprocess.run([sys.executable, "-m", "vault_write_mcp.server", "doctor", "--vault", str(root)],
                                                               env=env, capture_output=True, text=True).__dict__)
    assert result["returncode"] == 0 and json.loads(result["stdout"])["read_only"]
    assert not (root / ".git/vault-write").exists()


def test_doctor_invalid_vault_has_only_fixed_error_labels(tmp_path):
    root = tmp_path / "private-secret"
    root.mkdir()
    assert "private-secret" not in json.dumps(doctor(root))
    assert doctor(root)["vault"]["status"] == "invalid_or_unreadable"


def test_history_follow_renames_is_opt_in_and_marked_heuristic(root):
    store = VaultStore(root)
    original = store.write_note("old.md", "# Original\n" + "repeated\n" * 20, "missing", "old", "test")
    unrelated = store.write_note("similar.md", "# Similar\n" + "repeated\n" * 20, "missing", "similar", "test")
    plan = store.preview_move_note("old.md", "new.md")
    moved = store.move_note("old.md", "new.md", plan["expected_versions"], plan["plan_version"], "move", "test")
    default = store.note_history("new.md")
    assert set(default) == {"path", "commits"} and [entry["commit"] for entry in default["commits"]] == [moved["commit"]]
    followed = store.note_history("new.md", follow_renames=True)
    assert followed["rename_following"] == "git_similarity_heuristic"
    assert original["commit"] in [entry["commit"] for entry in followed["commits"]]
    assert unrelated["commit"] not in [entry["commit"] for entry in followed["commits"]]
    assert len(store.note_history("new.md", limit=1, follow_renames=True)["commits"]) == 1
    with pytest.raises(VaultError) as caught:
        store.note_history("new.md", follow_renames=1)
    assert caught.value.code == "INVALID_HISTORY"


def test_doctor_does_not_treat_literal_journal_paths_as_rule_syntax(root):
    process = multiprocessing.get_context("spawn").Process(target=crash, args=(str(root), "prepared"))
    process.start(); process.join(20)
    assert process.exitcode == 77
    journal = root / ".git/vault-write/pending.json"
    txn = json.loads(journal.read_bytes())
    txn["path"] = "a[1]*?.md"
    journal.write_text(json.dumps(txn))
    result = assert_read_only(root)
    assert result["journal"]["status"] == "pending" and result["journal"]["affected_count"] == 1


def crash_sqlite(root):
    db = sqlite3.connect(Path(root) / ".git/vault-write/receipts.sqlite3")
    db.execute("PRAGMA cache_size=1")
    db.execute("BEGIN IMMEDIATE")
    for index in range(3000):
        db.execute("INSERT INTO receipts VALUES (?, ?, ?)", (f"uncommitted-{index}", "0" * 64, "x" * 1000))
    os._exit(77)


def test_doctor_hot_rollback_journal_is_incomplete_and_never_recovered(root):
    store = VaultStore(root)
    process = multiprocessing.get_context("spawn").Process(target=crash_sqlite, args=(str(root),))
    process.start(); process.join(20)
    assert process.exitcode == 77
    rollback = store.state / "receipts.sqlite3-journal"
    assert rollback.exists() and rollback.stat().st_size > 512
    result = assert_read_only(root)
    assert result["receipts"]["rollback_journal_present"]
    assert not result["receipts"]["observation_complete"]


@pytest.mark.skipif(os.geteuid() == 0, reason="requires enforced non-root directory permissions")
def test_doctor_permission_denied_sidecars_are_unknown_not_path_leaks(root):
    store = VaultStore(root)
    before = snapshot(root)
    try:
        store.state.chmod(0)
        result = doctor(root)
        assert result["journal"]["status"] == "unsafe_or_unreadable"
        assert result["receipts"]["wal_present"] is None
        assert not result["receipts"]["observation_complete"]
        assert str(root) not in json.dumps(result)
    finally:
        store.state.chmod(0o700)
    assert snapshot(root) == before
