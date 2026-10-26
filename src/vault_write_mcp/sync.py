"""Snapshot synchronization. Lock order is sync.lock, then write.lock.

Only local snapshot/apply operations hold write.lock. Merge plumbing runs in a
disposable bare repository so vault attributes/configuration cannot run drivers.
"""

import base64
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import tempfile
import unicodedata

from .store import VaultError, _sync_directory


def validate_branch(store):
    branch = store._ref().removeprefix("refs/heads/")
    main = store.settings.sync.main_branch
    store._git("check-ref-format", "refs/heads/" + main)
    if branch == main or store.settings.push.branch not in ("", branch):
        raise VaultError("SYNC_CONFIGURATION", "Sync requires a separate writer branch; push.branch must name that branch or be empty.")
    return branch


def _target(store):
    return {"remote": store.settings.push.remote, "main_branch": store.settings.sync.main_branch,
            "writer_branch": store._ref().removeprefix("refs/heads/")}


def status(store):
    with store._locked():
        store._recover()
        path = store.state / "sync.json"
        last = json.loads(path.read_text()) if path.exists() else {"state": "idle"}
        if last.get("target") != _target(store):
            last = {"state": "idle"}
        head, pending = store._head(), len(store._pending_entries())
        current = store.settings.sync.enabled and last.get("published", False) and not pending and head == last.get("merged_commit")
        return {**last, "last_state": last["state"], "state": last["state"] if current or last["state"] != "synced" else "pending",
                "current": current, "target": _target(store), "enabled": store.settings.sync.enabled,
                "writer_commit": head, "pending_count": pending}


def _save(store, result):
    result = {**result, "target": result.get("target", _target(store)), "completed_at": datetime.now(timezone.utc).isoformat()}
    store._atomic_write(store.state / "sync.json", json.dumps(result, ensure_ascii=True).encode())
    return result


def _tree(store, commit):
    entries = {}
    for item in store._git("ls-tree", "-r", "-z", commit).split(b"\0"):
        if item:
            metadata, raw_path = item.split(b"\t", 1)
            mode, kind, blob = metadata.decode().split()
            try:
                name = raw_path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VaultError("UNSUPPORTED_TREE", "Git paths must be UTF-8.") from exc
            entries[name] = (mode, kind, blob)
    return entries


def _clean(store):
    if store._git("status", "--porcelain=v1", "--untracked-files=all"):
        raise VaultError("EXTERNAL_CHANGES", "Commit external edits, staged or untracked files before synchronization.")


def _path(store, name):
    parts = name.split("/")
    if PurePosixPath(name).is_absolute() or any(p in ("", ".", "..") or p.casefold() == ".git" for p in parts) or "\\" in name:
        raise VaultError("UNSUPPORTED_TREE", "Unsafe Git path.")
    path = store.root
    for index, part in enumerate(parts):
        path = path / part
        if index < len(parts) - 1 and path.exists() and not path.is_dir():
            raise VaultError("EXTERNAL_CHANGES", "A non-directory parent obstructs an incoming file.")
        if path.is_symlink():
            raise VaultError("EXTERNAL_CHANGES", "A symlink obstructs an incoming path.")
        if path != store.root and path.is_dir() and (path / ".git").exists():
            raise VaultError("EXTERNAL_CHANGES", "A nested repository obstructs an incoming path.")
    if path.exists() and not path.is_file():
        raise VaultError("EXTERNAL_CHANGES", "A directory obstructs an incoming file.")
    return path


def _bytes(path):
    return path.read_bytes() if path.exists() else None


def _checkout_entries(store, old_head, new_head, check_worktree=True):
    old, new = _tree(store, old_head), _tree(store, new_head)
    canonical = {}
    for name in old.keys() | new.keys():
        parts = name.split("/")
        if PurePosixPath(name).is_absolute() or "\\" in name or any(p in ("", ".", "..") or p.casefold().rstrip(". ") == ".git" for p in parts):
            raise VaultError("UNSUPPORTED_TREE", "Unsafe Git path.")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = unicodedata.normalize("NFC", prefix).casefold().rstrip(". ")
            if key in canonical and canonical[key] != prefix:
                raise VaultError("UNSUPPORTED_TREE", "Git paths collide under case or Unicode normalization.")
            canonical[key] = prefix
    entries = []
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        path = _path(store, name) if check_worktree else None
        if after is not None:
            store._check_atomic_device((store.root / name).parent)
        for item in (before, after):
            if item and (item[0] not in ("100644", "100755") or item[1] != "blob"):
                raise VaultError("UNSUPPORTED_TREE", "Only regular Git files can be applied.")
        for other in old.keys() | new.keys():
            if other != name and (other.startswith(name + "/") or name.startswith(other + "/")):
                raise VaultError("UNSUPPORTED_TREE", "File/directory transitions require operator intervention.")
        data = []
        for item in (before, after):
            content = store._git("cat-file", "blob", item[2]) if item else None
            data.append(None if content is None else base64.b64encode(content).decode())
        if path is not None and path.exists() and before is None:
            raise VaultError("EXTERNAL_CHANGES", "An existing file collides with an incoming note.")
        entries.append({"path": name, "old": data[0], "new": data[1],
                        "old_mode": before[0] if before else None, "new_mode": after[0] if after else None,
                        "file_mode": (path.stat().st_mode & 0o777) if path is not None and path.exists() else 0o644})
    return entries


def _verify_checkout(store, txn):
    conflict = lambda: VaultError("RECOVERY_CONFLICT", "Sync checkout conflicts with external changes; preserve pending.json for inspection.")
    if store._ref() != txn["ref"] or store._head() not in (txn["old_head"], txn["new_head"]) or store._pending_entries():
        raise conflict()
    # Index is replaced atomically by read-tree; only the complete old/new tree is valid.
    index_tree = store._git("write-tree").decode().strip()
    allowed_trees = {store._git("rev-parse", head + "^{tree}").decode().strip() for head in (txn["old_head"], txn["new_head"])}
    if index_tree not in allowed_trees:
        raise conflict()
    changed = {entry["path"] for entry in txn["entries"]}
    # Protect external changes even on paths untouched by this checkout.
    tracked = _tree(store, txn["old_head"])
    for name, (mode, kind, blob) in tracked.items():
        if name in changed:
            continue
        if kind == "blob" and mode in ("100644", "100755"):
            path = _path(store, name)
            if path.is_symlink() or not path.is_file() or path.read_bytes() != store._git("cat-file", "blob", blob):
                raise conflict()
    for entry in txn["entries"]:
        path = _path(store, entry["path"])
        if entry["new"] is not None:
            store._check_atomic_device(path.parent)
        current = _bytes(path)
        options = [None if entry[key] is None else base64.b64decode(entry[key]) for key in ("old", "new")]
        if current not in options:
            raise conflict()
        tracks_exec = store._git("config", "--bool", "--get", "core.filemode", allowed_codes=(0, 1)).strip() != b"false"
        mode_mask = 0o777 if tracks_exec else 0o666
        allowed_modes = {entry["file_mode"], entry["file_mode"] | 0o111 if entry["new_mode"] == "100755" else entry["file_mode"] & ~0o111}
        if path.exists() and (path.stat().st_mode & mode_mask) not in {mode & mode_mask for mode in allowed_modes}:
            raise conflict()


def recover_checkout(store, txn):
    _verify_checkout(store, txn)
    for entry in txn["entries"]:
        path = _path(store, entry["path"])
        if entry["new"] is None:
            if path.exists():
                path.unlink()
                _sync_directory(path.parent)
        else:
            mode = entry["file_mode"]
            if entry["old_mode"] != entry["new_mode"]:
                mode = mode | 0o111 if entry["new_mode"] == "100755" else mode & ~0o111
            store._atomic_write(path, base64.b64decode(entry["new"]), mode)
        store._checkpoint("sync_file_written")
    if store._head() == txn["old_head"]:
        store._git("update-ref", txn["ref"], txn["new_head"], txn["old_head"])
    store._checkpoint("sync_ref_updated")
    store._git("read-tree", txn["new_head"])
    store._checkpoint("sync_index_updated")
    store._clear_journal()


def synchronize(store):
    if not store.settings.sync.enabled:
        return {"state": "disabled", "enabled": False}
    fd = os.open(store.state / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _synchronize(store)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _synchronize(store):
    stage = "snapshot"
    result = {"state": "error", "published": False, "applied": False}
    try:
        store.flush()
        with store._locked():
            store._recover()
            branch = validate_branch(store)
            head, ref = store._head(), store._ref()
            result["writer_snapshot"] = head
            result["target"] = _target(store)
        remote, main = store.settings.push.remote, store.settings.sync.main_branch
        stage = "push_writer"
        store._git("push", "--porcelain", "--", remote, head + ":refs/heads/" + branch)
        stage = "fetch_main"
        store._git("fetch", "--no-tags", "--no-write-fetch-head", "--", remote,
                   "refs/heads/" + main + ":refs/vault-write/main")
        other = store._git("rev-parse", "refs/vault-write/main").decode().strip()
        result["main_snapshot"] = other
        stage = "merge"
        with tempfile.TemporaryDirectory(prefix="vault-write-merge-") as directory:
            isolated = Path(directory)
            store._git("init", "--bare", "--template=", str(isolated))
            store._git("fetch", "--no-tags", str(store.root), head, other, cwd=isolated)
            def ancestor(a, b):
                return store._git_run("merge-base", "--is-ancestor", a, b, cwd=isolated, allowed_codes=(0, 1)).returncode == 0
            if ancestor(head, other):
                merged = other
            elif ancestor(other, head):
                merged = head
            else:
                merge = store._git_run("merge-tree", "--write-tree", "--name-only", "-z", head, other,
                                       cwd=isolated, allowed_codes=(0, 1))
                chunks = merge.stdout.split(b"\0")
                if merge.returncode:
                    paths = []
                    for raw in chunks[1:]:
                        if not raw:
                            break
                        paths.append(raw.decode("utf-8", errors="replace"))
                    return _save(store, {**result, "state": "conflict", "stage": stage,
                                         "conflict_paths": [p[:512] for p in paths[:50]], "conflict_count": len(paths)})
                tree = chunks[0].decode().strip()
                merged = store._git("commit-tree", tree, "-p", other, "-p", head,
                                    data=b"Merge main and VaultWrite writer history\n", cwd=isolated).decode().strip()
            # Import immutable objects only; no index or working-tree mutation.
            store._git("fetch", "--no-tags", "--no-write-fetch-head", str(isolated), merged)
        result["merged_commit"] = merged
        stage = "preflight"
        _checkout_entries(store, head, merged, check_worktree=False)
        stage = "push_main"
        store._git("push", "--porcelain", "--", remote, merged + ":refs/heads/" + main)
        result["published"] = True
        stage = "apply"
        with store._locked():
            store._recover()
            if store._head() != head or store._ref() != ref or store._pending_entries():
                return _save(store, {**result, "state": "retry", "stage": stage})
            _clean(store)
            entries = _checkout_entries(store, head, merged)
            if merged != head:
                txn = {"kind": "sync_checkout", "old_head": head, "new_head": merged, "ref": ref, "entries": entries}
                _verify_checkout(store, txn)
                store._atomic_write(store.journal, json.dumps(txn).encode())
                store._checkpoint("sync_prepared")
                recover_checkout(store, txn)
            result["applied"] = True
        return _save(store, {**result, "state": "synced", "stage": "complete"})
    except VaultError as exc:
        # Never persist stderr: transport errors may embed credential-bearing URLs.
        return _save(store, {**result, "state": "blocked" if exc.code in ("EXTERNAL_CHANGES", "UNSUPPORTED_TREE", "RECOVERY_CONFLICT", "SYNC_CONFIGURATION") else "error",
                             "stage": stage, "error_code": exc.code})
    except OSError:
        return _save(store, {**result, "state": "error", "stage": stage, "error_code": "STORAGE_ERROR"})
