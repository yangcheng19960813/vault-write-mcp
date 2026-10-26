"""Preview guards, one-commit moves, durable receipts and real crash windows."""

import multiprocessing
import os
from pathlib import Path
import subprocess

import pytest

from vault_write_mcp.config import CommitConfig, Settings
from vault_write_mcp.store import VaultError, VaultStore, init_vault


@pytest.fixture
def store(tmp_path):
    init_vault(tmp_path)
    store = VaultStore(tmp_path)
    store.write_note("old.md", "source\n", "missing", "seed-source", "test")
    store.write_note("ref.md", "[[old#Heading|Display]] [label](old.md#fragment)\n", "missing", "seed-ref", "test")
    store.write_note("another.md", "![[old.md#^block]]\n", "missing", "seed-another", "test")
    return store


def apply(store, plan, request_id="move", **changes):
    arguments = {key: plan[key] for key in ("source", "destination", "expected_versions", "plan_version")}
    return store.move_note(**{**arguments, "request_id": request_id, "actor": "test", **changes})


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@localhost", *args], text=True).strip()


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(VaultError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def test_preview_is_read_only_exact_versions_and_one_move_commit(store):
    head = store._head()
    before = {name: store.read_note(name) for name in ("old.md", "ref.md", "another.md")}
    plan = store.preview_move_note("old.md", "archive/new.md")
    assert store._head() == head and not store.journal.exists()
    assert set(plan["expected_versions"]) == set(plan["affected_paths"]) == {"old.md", "archive/new.md", "ref.md", "another.md"}
    assert plan["expected_versions"]["archive/new.md"] == "missing" and plan["versions_after"]["old.md"] == "missing"
    assert plan["rewritten_links"] == 3 and plan["scan_count"] == 3
    assert {name: store.read_note(name) for name in before} == before
    result = apply(store, plan)
    assert result["versions"] == plan["versions_after"]
    assert store.read_note("old.md")["version"] == "missing"
    assert store.read_note("archive/new.md")["content"] == "source\n"
    assert store.read_note("ref.md")["content"] == "[[./archive/new.md#Heading|Display]] [label](./archive/new.md#fragment)\n"
    assert store.read_note("another.md")["content"] == "![[./archive/new.md#^block]]\n"
    assert git(store.root, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert git(store.root, "status", "--porcelain") == ""
    assert set(git(store.root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()) == set(plan["affected_paths"])


@pytest.mark.parametrize("mode", ["immediate", "interval"])
def test_move_receipt_replays_after_future_write_restart_and_flush(store, mode):
    configured = VaultStore(store.root, Settings(commit=CommitConfig(mode=mode)))
    plan = configured.preview_move_note("old.md", "new.md")
    first = apply(configured, plan)
    assert "pending" not in first and first["commit"] is not None
    configured.write_note("new.md", "future\n", first["versions"]["new.md"], "future", "test")
    restarted = VaultStore(store.root, configured.settings)
    restarted.flush()
    assert apply(restarted, plan) == {**first, "replayed": True}
    assert_error("IDEMPOTENCY_CONFLICT", apply, restarted, plan, actor="other")
    assert_error("IDEMPOTENCY_CONFLICT", apply, restarted, plan, destination="different.md")


def test_interval_pending_rejects_without_implicit_flush_then_one_immediate_move(store):
    configured = VaultStore(store.root, Settings(commit=CommitConfig(mode="interval")))
    old = configured.read_note("old.md")
    configured.append_note("old.md", "pending\n", old["version"], "append", "test")
    head = configured._head()
    assert_error("PENDING_BATCH", configured.preview_move_note, "old.md", "new.md")
    assert configured._head() == head and len(configured._pending_entries()) == 1
    configured.flush()
    before = configured._head()
    result = apply(configured, configured.preview_move_note("old.md", "new.md"))
    assert git(store.root, "rev-list", "--count", f"{before}..{result['commit']}") == "1"
    assert configured._pending_entries() == []


@pytest.mark.parametrize("change", ["source", "referrer", "new-backlink", "unrelated-topology"])
def test_preview_races_reject_before_any_move_mutation(store, change):
    plan = store.preview_move_note("old.md", "new.md")
    if change in ("source", "referrer"):
        name = "old.md" if change == "source" else "ref.md"
        snapshot = store.read_note(name)
        store.append_note(name, "changed\n", snapshot["version"], "racer", "test")
    else:
        store.write_note("new-note.md", "[[old]]\n" if change == "new-backlink" else "unrelated\n", "missing", "racer", "test")
    before = store._head()
    assert_error("PLAN_CONFLICT" if change == "unrelated-topology" else "VERSION_CONFLICT", apply, store, plan)
    assert store._head() == before and (store.root / "old.md").exists() and not (store.root / "new.md").exists()
    assert not store.journal.exists()


@pytest.mark.parametrize("mapping_change", ["missing", "extra", "stale"])
def test_expected_versions_must_exactly_cover_preview(store, mapping_change):
    plan = store.preview_move_note("old.md", "new.md")
    changed = dict(plan["expected_versions"])
    if mapping_change == "missing":
        changed.pop("ref.md")
    elif mapping_change == "extra":
        changed["other.md"] = "missing"
    else:
        changed["old.md"] = "sha256:" + "0" * 64
    assert_error("VERSION_CONFLICT", apply, store, plan, expected_versions=changed)
    assert (store.root / "old.md").exists() and not store.journal.exists()


@pytest.mark.parametrize("destination", ["old.md", "OLD.md", "ref.md", "new#name.md", "new|name.md", "../new.md", ".hidden/new.md"])
def test_move_paths_and_collisions_are_rejected(store, destination):
    with pytest.raises(VaultError):
        store.preview_move_note("old.md", destination)
    assert (store.root / "old.md").exists() and not store.journal.exists()


def test_case_and_unicode_directory_aliases_are_rejected(store):
    store.write_note("Café/note.md", "plain\n", "missing", "unicode", "test")
    assert_error("PATH_COLLISION", store.preview_move_note, "old.md", "Cafe\u0301/new.md")
    assert_error("PATH_COLLISION", store.preview_move_note, "old.md", "CAFÉ/new.md")


@pytest.mark.parametrize("obstruction", ["untracked", "ignored-md", "dirty", "staged", "symlink", "parent-file", "nested"])
def test_external_obstructions_rejected_without_overwrite(store, obstruction):
    destination = "new.md"
    if obstruction in ("untracked", "ignored-md"):
        (store.root / "unmanaged.md").write_text("external\n")
        if obstruction == "ignored-md":
            (store.root / ".git/info/exclude").write_text("unmanaged.md\n")
    elif obstruction in ("dirty", "staged"):
        (store.root / "ref.md").write_text("external\n")
        if obstruction == "staged":
            git(store.root, "add", "ref.md")
    elif obstruction == "symlink":
        (store.root / "link").symlink_to(store.root, target_is_directory=True)
        destination = "link/new.md"
    elif obstruction == "parent-file":
        destination = "old.md/new.md"
    else:
        (store.root / "nested").mkdir()
        git(store.root / "nested", "init")
        destination = "nested/new.md"
    with pytest.raises(VaultError):
        store.preview_move_note("old.md", destination)
    assert store.read_note("old.md")["content"] == "source\n" and not store.journal.exists()


def test_filter_attributes_rejected_before_preparation(store):
    (store.root / ".git/info/attributes").write_text("new.md filter=custom\n")
    assert_error("UNSUPPORTED_ATTRIBUTES", store.preview_move_note, "old.md", "new.md")
    assert not store.journal.exists()


def test_cross_device_preflight_rejects_before_journal(store, monkeypatch):
    def reject(parent):
        if parent.name == "archive":
            raise VaultError("UNSUPPORTED_FILESYSTEM", "different filesystem")
    monkeypatch.setattr(store, "_check_atomic_device", reject)
    assert_error("UNSUPPORTED_FILESYSTEM", store.preview_move_note, "old.md", "archive/new.md")
    assert not store.journal.exists() and not (store.root / "archive").exists()


@pytest.mark.parametrize("limit,value", [("MAX_SCAN_FILES", 1), ("MAX_SCAN_NOTES", 1), ("MAX_SCAN_BYTES", 1), ("MAX_AFFECTED_FILES", 2)])
def test_move_scan_and_change_budgets(store, monkeypatch, limit, value):
    from vault_write_mcp import moving
    monkeypatch.setattr(moving, limit, value)
    assert_error("MOVE_TOO_LARGE", store.preview_move_note, "old.md", "new.md")
    assert not store.journal.exists()


def crash_move(root, mode, stage):
    class CrashingStore(VaultStore):
        def _checkpoint(self, actual):
            if actual == stage:
                os._exit(77)
    configured = CrashingStore(root, Settings(commit=CommitConfig(mode=mode)))
    apply(configured, configured.preview_move_note("old.md", "archive/new.md"))


def kill_move(store, mode, stage):
    child = multiprocessing.get_context("spawn").Process(target=crash_move, args=(str(store.root), mode, stage))
    child.start()
    child.join(timeout=30)
    assert child.exitcode == 77


@pytest.mark.parametrize("mode", ["immediate", "interval"])
@pytest.mark.parametrize("stage", ["move_prepared", "move_file_written:archive/new.md", "move_file_written:another.md", "move_file_written:ref.md",
                                   "move_source_removed", "move_ref_updated", "move_index_updated", "move_receipt_saved"])
def test_multifile_move_recovers_once_after_actual_process_exit(store, mode, stage):
    plan = store.preview_move_note("old.md", "archive/new.md")
    before = store._head()
    kill_move(store, mode, stage)
    restarted = VaultStore(store.root, Settings(commit=CommitConfig(mode=mode)))
    replay = apply(restarted, plan)
    assert replay["replayed"] and not restarted.journal.exists()
    assert restarted.read_note("old.md")["version"] == "missing"
    assert restarted.read_note("archive/new.md")["content"] == "source\n"
    assert "archive/new.md" in restarted.read_note("ref.md")["content"]
    assert "archive/new.md" in restarted.read_note("another.md")["content"]
    assert git(store.root, "rev-list", "--count", f"{before}..HEAD") == "1"
    assert git(store.root, "status", "--porcelain") == ""
    assert not list(store.root.glob(".vault-write-*.tmp"))


@pytest.mark.parametrize("tampering", ["affected", "unaffected", "index", "new-backlink", "permissions"])
def test_recovery_verifies_all_files_before_any_overwrite(store, tampering):
    store.write_note("unaffected.md", "untouched\n", "missing", "untouched", "test")
    kill_move(store, "immediate", "move_prepared")
    if tampering in ("affected", "unaffected"):
        name = "ref.md" if tampering == "affected" else "unaffected.md"
        (store.root / name).write_text("external\n")
    elif tampering == "index":
        (store.root / "unaffected.md").write_text("staged\n")
        git(store.root, "add", "unaffected.md")
    elif tampering == "new-backlink":
        (store.root / "new-link.md").write_text("[[old]]\n")
    else:
        (store.root / "ref.md").chmod(0o700)
    assert_error("RECOVERY_CONFLICT", VaultStore, store.root)
    assert (store.root / "old.md").read_text() == "source\n"
    assert not (store.root / "archive/new.md").exists()
    assert store.journal.exists()


def test_ordinary_failure_retains_journal_then_rolls_forward(store, monkeypatch):
    plan = store.preview_move_note("old.md", "archive/new.md")
    original = store._atomic_write
    def fail(path, data, mode=0o600):
        if path.name == "ref.md":
            raise OSError("injected filesystem failure")
        return original(path, data, mode)
    monkeypatch.setattr(store, "_atomic_write", fail)
    assert_error("RECOVERY_REQUIRED", apply, store, plan)
    assert store.journal.exists() and (store.root / "old.md").exists()
    restarted = VaultStore(store.root)
    assert apply(restarted, plan)["replayed"] and not restarted.journal.exists()


def test_source_mode_and_untouched_bytes_preserved(store):
    snapshot = store.read_note("old.md")
    store.write_note("old.md", "\ufeffsource\r\nlast", snapshot["version"], "bom", "test")
    (store.root / "old.md").chmod(0o755)
    git(store.root, "add", "old.md")
    git(store.root, "commit", "-m", "Track executable mode")
    plan = store.preview_move_note("old.md", "archive/new.md")
    apply(store, plan)
    assert (store.root / "archive/new.md").read_bytes() == "\ufeffsource\r\nlast".encode()
    assert (store.root / "archive/new.md").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("tampering", ["destination-symlink", "source-symlink", "destination-directory", "deleted-referrer"])
def test_recovery_path_obstructions_report_conflict_without_partial_writes(store, tampering):
    kill_move(store, "immediate", "move_prepared")
    if tampering == "destination-symlink":
        (store.root / "archive").mkdir()
        (store.root / "archive/new.md").symlink_to(store.root / "ref.md")
    elif tampering == "source-symlink":
        (store.root / "old.md").unlink()
        (store.root / "old.md").symlink_to(store.root / "ref.md")
    elif tampering == "destination-directory":
        (store.root / "archive/new.md").mkdir(parents=True)
    else:
        (store.root / "ref.md").unlink()
    ref = (store.root / "another.md").read_bytes()
    assert_error("RECOVERY_CONFLICT", VaultStore, store.root)
    assert (store.root / "another.md").read_bytes() == ref and store.journal.exists()


@pytest.mark.parametrize("destination", ["new?query.md", "new`code.md"])
def test_move_rejects_query_and_code_delimiters_in_destination(store, destination):
    assert_error("INVALID_PATH", store.preview_move_note, "old.md", destination)


@pytest.mark.parametrize("name", ["ref(1).md", "notes/ref#part.md", "notes/ref[1].md", "notes/ref?query.md"])
def test_referrer_paths_use_consistent_safe_rules_in_plan_apply_and_recovery(store, name):
    store.write_note(name, "[[old]]\n", "missing", "unusual-referrer", "test")
    plan = store.preview_move_note("old.md", "new.md")
    assert name in plan["expected_versions"]
    result = apply(store, plan)
    restarted = VaultStore(store.root)
    assert "new.md" in restarted.read_note(name)["content"]
    assert apply(restarted, plan) == {**result, "replayed": True}
    assert not restarted.journal.exists()


def test_preview_contains_bounded_target_changes_without_full_note_dump(store):
    plan = store.preview_move_note("old.md", "new.md")
    assert len(plan["link_changes"]) == plan["rewritten_links"] == 3
    assert not plan["link_changes_truncated"]
    for item in plan["link_changes"]:
        assert set(item) == {"path", "result_path", "line", "old_target", "new_target"}
        assert "old" in item["old_target"] and "new" in item["new_target"]
    current = store.read_note("ref.md")
    store.write_note("ref.md", " ".join("[[old]]" for _ in range(100)), current["version"], "many", "test")
    limited = store.preview_move_note("old.md", "new.md")
    assert limited["link_changes_truncated"] and len(limited["link_changes"]) == 50
    assert limited["rewritten_links"] == 101


def test_global_link_scan_budget_rejects_preflight(store, monkeypatch):
    from vault_write_mcp import moving
    monkeypatch.setattr(moving, "MAX_SCAN_LINKS", 1)
    assert_error("MOVE_TOO_LARGE", store.preview_move_note, "old.md", "new.md")
    assert not store.journal.exists()


def test_move_paths_require_valid_utf8(store):
    assert_error("INVALID_PATH", store.preview_move_note, "old.md", "bad\ud800.md")
