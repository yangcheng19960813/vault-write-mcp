"""Actual process exits inside atomic replacement, including its ownership gap."""

import multiprocessing
import os
from pathlib import Path
import tempfile

import pytest

from vault_write_mcp.config import CommitConfig, Settings
from vault_write_mcp.store import VaultError, VaultStore, init_vault


def legacy_orphan(root):
    # The old implementation's mkstemp + finally cannot survive process death.
    fd, name = tempfile.mkstemp(prefix=".vault-write-", suffix=".tmp", dir=root)
    os.write(fd, b"unfinished")
    os.fsync(fd)
    os._exit(77)


def crash_inside_atomic(root, mode, target, stage):
    class CrashingStore(VaultStore):
        def _atomic_write(self, path, data, mode=0o600):
            self.atomic_target = "note" if path.name == "note.md" else "journal"
            super()._atomic_write(path, data, mode)

        def _checkpoint(self, actual):
            if actual == stage and getattr(self, "atomic_target", None) == target:
                os._exit(77)

    store = CrashingStore(root, Settings(commit=CommitConfig(mode=mode)))
    old = store.read_note("note.md")
    store.append_note("note.md", "durable\n", old["version"], "crash", "test")


def run_child(function, *args):
    process = multiprocessing.get_context("spawn").Process(target=function, args=args)
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 77


def test_reproduces_legacy_note_directory_orphan(tmp_path):
    run_child(legacy_orphan, str(tmp_path))
    leftovers = list(tmp_path.iterdir())
    assert len(leftovers) == 1 and leftovers[0].name.startswith(".vault-write-")
    assert leftovers[0].read_bytes() == b"unfinished"


@pytest.mark.parametrize("mode", ["immediate", "interval"])
@pytest.mark.parametrize("target", ["journal", "note"])
@pytest.mark.parametrize("stage", ["atomic_created", "atomic_registered", "atomic_fsynced", "atomic_replaced"])
def test_atomic_crash_recovery_leaks_no_visible_temp_and_retries_once(tmp_path, mode, target, stage):
    init_vault(tmp_path)
    original = VaultStore(tmp_path).write_note("note.md", "old\n", "missing", "seed", "test")
    run_child(crash_inside_atomic, str(tmp_path), mode, target, stage)
    assert sorted(path.name for path in tmp_path.iterdir()) == [".git", "note.md"]
    configured = Settings(commit=CommitConfig(mode=mode))
    restarted = VaultStore(tmp_path, configured)
    first = restarted.append_note("note.md", "durable\n", original["version"], "crash", "test")
    assert restarted.append_note("note.md", "durable\n", original["version"], "crash", "test")["replayed"]
    assert restarted.read_note("note.md")["content"] == "old\ndurable\n"
    restarted.flush()
    assert len(restarted.note_history("note.md")["commits"]) == 2
    assert not restarted.journal.exists()
    with restarted._db() as db:
        assert db.execute("SELECT * FROM atomic_files").fetchall() == []
    private = list(restarted.state.glob("atomic-*.tmp"))
    # Before registration no ownership can be proved: retain, only in .git.
    assert len(private) == (1 if stage == "atomic_created" else 0)
    assert first["replayed"] == (target == "note" or stage == "atomic_replaced")


def test_cleanup_requires_exact_durable_inode_and_preserves_unknown_paths(tmp_path):
    init_vault(tmp_path)
    store = VaultStore(tmp_path)
    visible = tmp_path / ".vault-write-user.tmp"
    visible.write_bytes(b"user's unrelated file")
    unknown = store.state / "atomic-unknown.tmp"
    unknown.write_bytes(b"unknown")
    owned = store.state / "atomic-owned.tmp"
    owned.write_bytes(b"owned")
    replaced = store.state / "atomic-replaced.tmp"
    replaced.write_bytes(b"old")
    old = replaced.stat()
    # Keep the old inode alive so this test cannot accidentally reuse it.
    moved = store.state / "keep-old"
    replaced.rename(moved)
    replaced.write_bytes(b"replacement")
    symlink = store.state / "atomic-link.tmp"
    symlink.symlink_to(visible)
    hardlink = store.state / "atomic-hardlink.tmp"
    os.link(visible, hardlink)
    with store._db() as db:
        for path, info in ((owned, owned.stat()), (replaced, old), (symlink, symlink.stat()), (hardlink, hardlink.stat())):
            db.execute("INSERT INTO atomic_files VALUES (?, ?, ?)", (path.name, info.st_dev, info.st_ino))
    restarted = VaultStore(tmp_path)
    assert not owned.exists()
    assert visible.read_bytes() == b"user's unrelated file"
    assert unknown.read_bytes() == b"unknown"
    assert replaced.read_bytes() == b"replacement"
    assert symlink.is_symlink() and hardlink.read_bytes() == visible.read_bytes()
    restarted.write_note("note.md", "works\n", "missing", "normal", "test")


def test_cross_device_preflight_happens_before_target_or_journal_mutation(tmp_path, monkeypatch):
    init_vault(tmp_path)
    store = VaultStore(tmp_path)
    head = store._head()
    actual_stat = Path.stat

    def different_device(path, *args, **kwargs):
        info = actual_stat(path, *args, **kwargs)
        if path == store.root:
            values = list(info)
            values[2] = info.st_dev + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "stat", different_device)
    with pytest.raises(VaultError, match="share a filesystem") as caught:
        store.write_note("new/note.md", "x\n", "missing", "cross-device", "test")
    assert caught.value.code == "UNSUPPORTED_FILESYSTEM"
    assert not (tmp_path / "new").exists() and not store.journal.exists()
    assert store._head() == head
