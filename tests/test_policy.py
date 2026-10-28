from dataclasses import replace
import json
import multiprocessing
import os
import sqlite3
import subprocess

import pytest

from vault_write_mcp.config import CommitConfig, Settings, WritePolicyConfig, load_settings
from vault_write_mcp.store import VaultError, VaultStore, init_vault


def configured(root, allow=(), deny=(), interval=False):
    return VaultStore(root, Settings(write_policy=WritePolicyConfig(allow, deny),
                                    commit=CommitConfig(mode="interval" if interval else "immediate")))


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def root(tmp_path):
    init_vault(tmp_path)
    VaultStore(tmp_path).write_note("core/note.md", "one\ntwo\n", "missing", "seed", "test")
    return tmp_path


@pytest.mark.parametrize("operation", ["write", "append", "patch"])
def test_all_single_file_entries_reject_without_mutation(root, operation):
    store = configured(root, deny=["CORE/"])
    note = store.read_note("core/note.md")
    anchor = store.read_note_range("core/note.md")["lines"][0].split("|", 1)[0]
    before = snapshot(root)
    content = [{"op": "SWAP", "anchor": anchor, "lines": ["bad"]}] if operation == "patch" else "bad"
    with pytest.raises(VaultError) as caught:
        getattr(store, operation + "_note")("core/note.md", content, note["version"], "denied", "test")
    assert caught.value.code == "PATH_PROTECTED"
    assert snapshot(root) == before


def test_policy_boundaries_normalization_and_deny_precedence(root):
    store = configured(root, allow=["core", "CAFÉ"], deny=["core/private"])
    for path in ("core/new.md", "cafe\u0301/note.md"):
        assert store.write_note(path, "ok", "missing", path.replace("/", "-").encode().hex()[:128], "test")["changed"]
    for path in ("core-other/note.md", "CORE/PRIVATE/note.md", "other.md"):
        with pytest.raises(VaultError) as caught:
            store.write_note(path, "no", "missing", "denied", "test")
        assert caught.value.code == "PATH_PROTECTED"
    # A deny-only policy permits the sibling, not aliases of the protected path.
    deny = configured(root, deny=["café", "core"])
    deny.write_note("core-other/note.md", "yes", "missing", "sibling", "test")
    for path in ("CORE/note.md", "CAFE\u0301/note.md"):
        with pytest.raises(VaultError, match="policy"):
            deny.write_note(path, "no", "missing", "deny-alias", "test")


@pytest.mark.parametrize("value", ["", "/root", "a//b", "a/../b", "./a", "a\\b", "a/*", "a?", ".git", "a\x00b", 3, None])
def test_bad_policy_rule_rejected(value):
    with pytest.raises(ValueError):
        WritePolicyConfig(deny_paths=[value])


@pytest.mark.parametrize("value", ["core", True, 1, {}, ["a"] * 257])
def test_bad_policy_array_rejected(value):
    with pytest.raises(ValueError):
        WritePolicyConfig(allow_paths=value)


def test_config_default_strictness_and_replay_after_policy_restoration(root):
    store = VaultStore(root)
    args = ("core/note.md", "accepted\n", store.read_note("core/note.md")["version"], "receipt", "test")
    result = store.write_note(*args)
    protected = configured(root, deny=["core"])
    before = snapshot(root)
    with pytest.raises(VaultError) as caught:
        protected.write_note(*args)
    assert caught.value.code == "PATH_PROTECTED" and snapshot(root) == before
    assert VaultStore(root).write_note(*args) == {**result, "replayed": True}
    path = root / ".git/config.toml"
    path.write_text('[write_policy]\nallow_paths=["core/"]\ndeny_paths=["core/private"]\n')
    assert load_settings(path).write_policy == WritePolicyConfig(["CORE"], ["core/private"])
    path.write_text('[write_policy]\nunknown=[]')
    with pytest.raises(ValueError):
        load_settings(path)


@pytest.mark.parametrize("denied", ["core", "target", "refs"])
def test_move_preview_and_execute_guard_every_affected_path(root, denied):
    initial = VaultStore(root)
    initial.write_note("refs/link.md", "[[core/note]]\n", "missing", "link", "test")
    plan = initial.preview_move_note("core/note.md", "target/note.md")
    store = configured(root, deny=[denied])
    before = snapshot(root)
    for call in (lambda: store.preview_move_note("core/note.md", "target/note.md"),
                 lambda: store.move_note("core/note.md", "target/note.md", plan["expected_versions"], plan["plan_version"], "move", "test")):
        with pytest.raises(VaultError) as caught:
            call()
        assert caught.value.code == "PATH_PROTECTED" and snapshot(root) == before


def test_completed_move_receipt_not_bypass_and_can_replay_after_restoration(root):
    store = VaultStore(root)
    store.write_note("refs/link.md", "[[core/note]]", "missing", "ref", "test")
    plan = store.preview_move_note("core/note.md", "target/note.md")
    args = ("core/note.md", "target/note.md", plan["expected_versions"], plan["plan_version"], "move", "test")
    result = store.move_note(*args)
    protected = configured(root, deny=["refs"])
    before = snapshot(root)
    with pytest.raises(VaultError) as caught:
        protected.move_note(*args)
    assert caught.value.code == "PATH_PROTECTED" and snapshot(root) == before
    assert VaultStore(root).move_note(*args) == {**result, "replayed": True}


def crash(root, kind):
    class Crash(VaultStore):
        def _checkpoint(self, stage):
            target = "move_file_written:target/note.md" if kind == "move" else "prepared"
            if stage == target:
                if kind == "batch_commit" and json.loads(self.journal.read_text()).get("kind") != "batch_commit":
                    return
                os._exit(77)
    interval = kind in ("pending_write", "batch_commit")
    store = Crash(root, Settings(commit=CommitConfig(mode="interval" if interval else "immediate")))
    if kind == "move":
        plan = store.preview_move_note("core/note.md", "target/note.md")
        store.move_note("core/note.md", "target/note.md", plan["expected_versions"], plan["plan_version"], "crash", "test")
    else:
        store.append_note("core/note.md", "new", store.read_note("core/note.md")["version"], "crash", "test")
        if kind == "batch_commit":
            store.flush()


@pytest.mark.parametrize("kind", ["write", "move", "pending_write", "batch_commit"])
def test_new_policy_blocks_whole_interrupted_recovery_before_constructor_writes(root, kind):
    process = multiprocessing.get_context("spawn").Process(target=crash, args=(str(root), kind))
    process.start(); process.join(20)
    assert process.exitcode == 77
    # Legacy DB schema gap proves constructor cannot create tables first.
    with sqlite3.connect(root / ".git/vault-write/receipts.sqlite3") as db:
        db.execute("DROP TABLE atomic_files")
    before = snapshot(root)
    with pytest.raises(VaultError) as caught:
        configured(root, deny=["core"])
    assert caught.value.code == "PATH_PROTECTED" and snapshot(root) == before
    restored = VaultStore(root)
    assert not restored.journal.exists()


def test_policy_blocks_flush_of_previously_accepted_interval_batch(root):
    store = configured(root, interval=True)
    store.write_note("new.md", "pending", "missing", "pending", "test")
    protected = configured(root, deny=["new.md"], interval=True)
    before = snapshot(root)
    with pytest.raises(VaultError) as caught:
        protected.flush()
    assert caught.value.code == "PATH_PROTECTED" and snapshot(root) == before
    assert store.flush()["changed"]


@pytest.mark.parametrize("path", ["a[1].md", "why?.md", "star*.md"])
def test_policy_candidates_keep_literal_existing_filename_contract(root, path):
    store = configured(root, deny=["private"])
    created = store.write_note(path, "yes", "missing", "literal", "test")
    assert store.append_note(path, "!", created["version"], "append-literal", "test")["changed"]


def test_omitted_move_backlink_policy_check_precedes_owned_atomic_cleanup(root):
    initial = VaultStore(root)
    initial.write_note("refs/link.md", "[[core/note]]", "missing", "ref", "test")
    plan = initial.preview_move_note("core/note.md", "target/note.md")
    protected = configured(root, deny=["refs"])
    temporary = protected.state / "atomic-retained.tmp"
    temporary.write_bytes(b"must survive rejected request")
    info = temporary.stat()
    with protected._db() as db:
        db.execute("INSERT INTO atomic_files VALUES (?, ?, ?)", (temporary.name, info.st_dev, info.st_ino))
    expected = {name: value for name, value in plan["expected_versions"].items() if name != "refs/link.md"}
    before = snapshot(root)
    for call in (lambda: protected.preview_move_note("core/note.md", "target/note.md"),
                 lambda: protected.move_note("core/note.md", "target/note.md", expected, plan["plan_version"], "move", "test")):
        with pytest.raises(VaultError) as caught:
            call()
        assert caught.value.code == "PATH_PROTECTED" and snapshot(root) == before
