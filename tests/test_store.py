from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import subprocess

import pytest

from vault_write_mcp.store import VaultError, VaultStore, init_vault


def git(root, *args):
    return subprocess.check_output(["git", "--literal-pathspecs", "-C", str(root), *args]).decode().strip()


@pytest.fixture
def store(tmp_path):
    init_vault(tmp_path)
    return VaultStore(tmp_path)


def seed(store, path="note.md", text="# Note\n"):
    return store.write_note(path, text, "missing", "seed", "test")


def assert_error(code, method, *args):
    with pytest.raises(VaultError) as caught:
        method(*args)
    assert caught.value.code == code


def parallel_append(root, barrier, queue, actor, retry):
    try:
        store = VaultStore(root)
        read = store.read_note("note.md")
        barrier.wait(timeout=20)
        try:
            store.append_note("note.md", actor + "\n", read["version"], actor, actor)
            queue.put((actor, "success"))
        except VaultError as exc:
            if retry and exc.code == "VERSION_CONFLICT":
                current = store.read_note("note.md")
                store.append_note("note.md", actor + "\n", current["version"], actor + "-retry", actor)
                queue.put((actor, "retried"))
            else:
                queue.put((actor, exc.code))
    except BaseException as exc:
        queue.put((actor, repr(exc)))


def crash_write(root, stage, creating):
    class CrashingStore(VaultStore):
        def _checkpoint(self, current_stage):
            if stage == current_stage:
                os._exit(77)

    store = CrashingStore(root)
    if creating:
        store.write_note("nested/new.md", "new\n", "missing", "crash", "test")
    else:
        store.append_note("note.md", "new\n", store.read_note("note.md")["version"], "crash", "test")


def test_create_replace_and_git_history(store):
    assert store.read_note("note.md")["version"] == "missing"
    first = seed(store)
    assert len(first["commit"]) == 40
    assert store.read_note("note.md")["content"] == "# Note\n"
    next_write = store.write_note("note.md", "# Changed\n", first["version"], "replace", "codex")
    assert git(store.root, "show", f"{first['commit']}:note.md") == "# Note"
    assert git(store.root, "show", f"{next_write['commit']}:note.md") == "# Changed"
    assert "VaultWrite-Actor: codex" in git(store.root, "show", "-s", "--format=%B", "HEAD")
    assert len(store.note_history("note.md")["commits"]) == 2
    assert git(store.root, "status", "--porcelain") == ""


@pytest.mark.parametrize("bad", ["", "blind", "*", None, "sha256:bad"])
def test_version_cannot_be_bypassed(store, bad):
    assert_error("INVALID_VERSION", store.write_note, "note.md", "bad", bad, "bad", "test")
    assert not (store.root / "note.md").exists()
    assert git(store.root, "rev-list", "--count", "HEAD") == "1"


def test_stale_write_and_create_are_rejected(store):
    first = seed(store)
    store.append_note("note.md", "A\n", first["version"], "A", "A")
    head = git(store.root, "rev-parse", "HEAD")
    assert_error("VERSION_CONFLICT", store.write_note, "note.md", "B\n", first["version"], "B", "B")
    assert_error("VERSION_CONFLICT", store.write_note, "note.md", "B\n", "missing", "create-again", "B")
    assert git(store.root, "rev-parse", "HEAD") == head
    assert store.read_note("note.md")["content"] == "# Note\nA\n"


@pytest.mark.parametrize("retry", [False, True])
def test_cross_process_same_note_conflict_and_retry(store, retry):
    seed(store)
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=parallel_append, args=(str(store.root), barrier, queue, actor, retry)) for actor in ("A", "B")]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert sorted(result[1] for result in results) == (["retried", "success"] if retry else ["VERSION_CONFLICT", "success"])
    content = store.read_note("note.md")["content"]
    if retry:
        assert content.count("A\n") == content.count("B\n") == 1
        assert git(store.root, "rev-list", "--count", "HEAD") == "4"
    else:
        assert ("A\n" in content) != ("B\n" in content)
    assert git(store.root, "status", "--porcelain") == ""


def test_concurrent_different_notes_each_have_one_commit(store):
    def write(i):
        return VaultStore(store.root).write_note(f"notes/{i}.md", str(i), "missing", f"parallel-{i}", "test")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(12)))
    assert len({result["commit"] for result in results}) == 12
    assert git(store.root, "rev-list", "--count", "HEAD") == "13"
    for i, result in enumerate(results):
        assert store.read_note(f"notes/{i}.md")["content"] == str(i)
        assert git(store.root, "diff-tree", "--no-commit-id", "--name-only", "-r", result["commit"]) == f"notes/{i}.md"
    assert git(store.root, "status", "--porcelain") == ""


def test_idempotency_survives_restart_and_later_writes(store):
    first = seed(store)
    arguments = ("note.md", "once\n", first["version"], "once", "test")
    result = store.append_note(*arguments)
    store.append_note("note.md", "later\n", result["version"], "later", "test")
    before = git(store.root, "rev-parse", "HEAD")
    replay = VaultStore(store.root).append_note(*arguments)
    assert replay == {**result, "replayed": True}
    assert store.read_note("note.md")["content"] == "# Note\nonce\nlater\n"
    assert git(store.root, "rev-parse", "HEAD") == before
    assert_error("IDEMPOTENCY_CONFLICT", store.append_note, "note.md", "changed", first["version"], "once", "test")


def test_concurrent_identical_request_is_only_applied_once(store):
    first = seed(store)
    def append(_):
        return VaultStore(store.root).append_note("note.md", "once\n", first["version"], "same-request", "test")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(append, range(4)))
    assert sum(not result["replayed"] for result in results) == 1
    assert store.read_note("note.md")["content"].count("once\n") == 1
    assert git(store.root, "rev-list", "--count", "HEAD") == "3"


def test_noop_and_empty_note(store):
    first = seed(store, text="")
    result = store.append_note("note.md", "", first["version"], "noop", "test")
    assert not result["changed"]
    assert result["commit"] == first["commit"]
    assert store.append_note("note.md", "", first["version"], "noop", "test")["replayed"]
    assert git(store.root, "rev-list", "--count", "HEAD") == "2"


@pytest.mark.parametrize("path", ["nested/中文 笔记.md", "notes/a,b.md", "notes/[x].md", "notes/a:b.md"])
def test_unicode_and_literal_paths_preserve_bytes(store, path):
    first = seed(store, path, "\ufeff# 中文\r\n")
    store.append_note(path, "追加\r\n", first["version"], "append", "test")
    assert (store.root / path).read_bytes() == "\ufeff# 中文\r\n追加\r\n".encode()
    assert git(store.root, "status", "--porcelain") == ""


@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", ".git/config.md", "a/../b.md", "a//b.md", "a\\b.md", "a\nb.md", "note.txt"])
def test_unsafe_paths_are_rejected(store, path):
    assert_error("INVALID_PATH", store.write_note, path, "bad", "missing", "bad", "test")


def test_symlink_and_nested_repository_are_rejected(store, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (store.root / "link").symlink_to(outside, target_is_directory=True)
    assert_error("INVALID_PATH", store.write_note, "link/note.md", "bad", "missing", "link", "test")
    nested = store.root / "nested"
    init_vault(nested)
    assert_error("INVALID_PATH", store.write_note, "nested/note.md", "bad", "missing", "nested", "test")
    assert not (outside / "note.md").exists()


def test_unrelated_worktree_edits_are_preserved_and_not_committed(store):
    seed(store, "other.md", "committed")
    (store.root / "other.md").write_text("local edit")
    (store.root / "untracked.txt").write_text("keep")
    result = store.write_note("note.md", "new", "missing", "new", "test")
    assert git(store.root, "show", f"{result['commit']}:other.md") == "committed"
    assert (store.root / "other.md").read_text() == "local edit"
    assert (store.root / "untracked.txt").read_text() == "keep"


def test_staged_changes_and_dirty_target_fail_without_changes(store):
    first = seed(store)
    (store.root / "other.md").write_text("staged")
    git(store.root, "add", "other.md")
    assert_error("STAGED_CHANGES", store.append_note, "note.md", "bad", first["version"], "staged", "test")
    assert git(store.root, "diff", "--cached", "--name-only") == "other.md"
    git(store.root, "reset", "--", "other.md")
    (store.root / "note.md").write_text("external")
    current = store.read_note("note.md")
    assert_error("DIRTY_TARGET", store.write_note, "note.md", "bad", current["version"], "dirty", "test")
    assert (store.root / "note.md").read_text() == "external"


@pytest.mark.parametrize("git_command", ["commit-tree", "update-ref"])
def test_git_failure_never_reports_success_and_preserves_content(store, monkeypatch, git_command):
    first = seed(store)
    original = store._git
    def fail(*args, **kwargs):
        if args[0] == git_command:
            raise VaultError("GIT_ERROR", "injected failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "_git", fail)
    with pytest.raises(VaultError):
        store.append_note("note.md", "bad", first["version"], "failed", "test")
    assert store.read_note("note.md")["content"] == "# Note\n"
    assert git(store.root, "rev-parse", "HEAD") == first["commit"]
    assert not store.journal.exists()
    monkeypatch.setattr(store, "_git", original)
    assert not store.append_note("note.md", "bad", first["version"], "failed", "test")["replayed"]


@pytest.mark.parametrize("stage", ["prepared", "file_written", "ref_updated", "receipt_saved"])
@pytest.mark.parametrize("creating", [False, True])
def test_actual_process_crash_is_recovered_exactly_once(store, stage, creating):
    first = seed(store)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=crash_write, args=(str(store.root), stage, creating))
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 77
    restarted = VaultStore(store.root)
    if creating:
        result = restarted.write_note("nested/new.md", "new\n", "missing", "crash", "test")
        assert restarted.read_note("nested/new.md")["content"] == "new\n"
    else:
        result = restarted.append_note("note.md", "new\n", first["version"], "crash", "test")
        assert restarted.read_note("note.md")["content"] == "# Note\nnew\n"
    assert result["replayed"]
    assert not restarted.journal.exists()
    assert git(store.root, "rev-list", "--count", "HEAD") == "3"
    assert git(store.root, "status", "--porcelain") == ""
    git(store.root, "fsck", "--no-dangling")


def test_recovery_preserves_conflicting_external_edits(store):
    seed(store)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=crash_write, args=(str(store.root), "file_written", False))
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 77
    (store.root / "note.md").write_text("external edit")
    assert_error("RECOVERY_CONFLICT", VaultStore, store.root)
    assert (store.root / "note.md").read_text() == "external edit"
    assert store.journal.exists()


def test_size_encoding_and_missing_append(store):
    assert_error("TOO_LARGE", store.write_note, "note.md", "x" * (store.MAX_BYTES + 1), "missing", "large", "test")
    assert_error("INVALID_ENCODING", store.write_note, "note.md", "\ud800", "missing", "encoding", "test")
    assert_error("NOT_FOUND", store.append_note, "note.md", "text", "missing", "absent", "test")


def test_init_refuses_nonempty_directories(tmp_path):
    (tmp_path / "note.md").write_text("keep")
    assert_error("INVALID_VAULT", init_vault, tmp_path)
    assert (tmp_path / "note.md").read_text() == "keep"


def test_index_failure_after_commit_requires_recovery_and_replay(store, monkeypatch):
    first = seed(store)
    original = store._git
    def fail_index(*args, **kwargs):
        if args[0] == "update-index" and kwargs.get("index") is None:
            raise VaultError("GIT_ERROR", "injected index failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "_git", fail_index)
    arguments = ("note.md", "once\n", first["version"], "index-failure", "test")
    assert_error("RECOVERY_REQUIRED", store.append_note, *arguments)
    assert store.journal.exists()
    assert git(store.root, "rev-list", "--count", "HEAD") == "3"
    monkeypatch.setattr(store, "_git", original)
    result = VaultStore(store.root).append_note(*arguments)
    assert result["replayed"]
    assert store.read_note("note.md")["content"] == "# Note\nonce\n"
    assert git(store.root, "rev-list", "--count", "HEAD") == "3"
    assert git(store.root, "status", "--porcelain") == ""


def test_recovery_preserves_external_staged_deletion(store):
    seed(store)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=crash_write, args=(str(store.root), "file_written", False))
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 77
    git(store.root, "rm", "--cached", "note.md")
    assert_error("RECOVERY_CONFLICT", VaultStore, store.root)
    assert git(store.root, "diff", "--cached", "--name-status") == "D\tnote.md"
    assert (store.root / "note.md").read_text() == "# Note\nnew\n"


def test_git_filter_is_rejected_without_running_it(store):
    (store.root / ".gitattributes").write_text("*.md filter=custom\n")
    assert_error("UNSUPPORTED_ATTRIBUTES", store.write_note, "note.md", "bad", "missing", "filtered", "test")
    assert not (store.root / "note.md").exists()


def test_historical_content_can_be_restored_through_conditional_write(store):
    first = seed(store)
    second = store.write_note("note.md", "replacement\n", first["version"], "replace", "test")
    historical = subprocess.check_output(["git", "-C", str(store.root), "show", f"{first['commit']}:note.md"]).decode()
    restored = store.write_note("note.md", historical, second["version"], "restore", "test")
    assert restored["commit"] not in (first["commit"], second["commit"])
    assert store.read_note("note.md")["content"] == "# Note\n"
    assert len(store.note_history("note.md")["commits"]) == 3
