"""Deferred file transactions and coordinated batch commits under the vault lock."""

import base64
import json
import os
from pathlib import Path
import stat
import tempfile

from .store import VaultError, version


def _save_write(store, txn):
    with store._db() as db:
        db.execute("INSERT OR REPLACE INTO pending VALUES (?, ?)", (txn["path"], json.dumps(txn["pending"])))
        db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?)", (txn["request_id"], txn["fingerprint"], json.dumps(txn["result"])))
        db.execute("INSERT OR IGNORE INTO pending_requests VALUES (?, ?)", (txn["request_id"], txn["path"]))


def defer_write(store, path, old, new, head, ref, old_blob, git_mode, request_id, fingerprint, result, actor):
    target = store._path(path)
    previous = store._pending_entry(path)
    pending = {"path": path, "ref": ref, "old_blob": old_blob, "git_mode": git_mode, "version": version(new),
               "actors": sorted(set((previous or {}).get("actors", []) + [actor]))}
    result = {**result, "commit": None, "pending": True}
    txn = {"kind": "pending_write", "path": path, "old": None if old is None else base64.b64encode(old).decode(),
           "new": base64.b64encode(new).decode(), "old_head": head, "ref": ref,
           "file_mode": stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644,
           "previous_pending": previous, "pending": pending, "request_id": request_id, "fingerprint": fingerprint, "result": result}
    store._atomic_write(store.journal, json.dumps(txn).encode())
    store._checkpoint("prepared")
    try:
        store._atomic_write(target, new, txn["file_mode"])
        store._checkpoint("file_written")
        _save_write(store, txn)
        store._checkpoint("receipt_saved")
        store._clear_journal()
    except Exception as exc:
        raise VaultError("RECOVERY_REQUIRED", "The deferred write is pending recovery; retry the identical request_id.") from exc
    return result


def _check_index(store, entry):
    staged = store._git("ls-files", "--stage", "-z", "--", entry["path"])
    if staged:
        metadata = staged.split(b"\t", 1)[0].decode().split()
        if len(staged.split(b"\0")) != 2 or metadata[0] != entry["git_mode"] or metadata[1] not in (entry["old_blob"], entry["blob"]) or metadata[2] != "0":
            raise VaultError("RECOVERY_CONFLICT", "A batch index entry changed outside VaultWrite.")
    elif entry["old_blob"] is not None:
        raise VaultError("RECOVERY_CONFLICT", "A batch index entry was removed outside VaultWrite.")


def _finish_batch(store, entries, commit):
    with store._db() as db:
        for entry in entries:
            rows = db.execute("SELECT receipts.request_id, receipts.result FROM receipts JOIN pending_requests USING (request_id) WHERE pending_requests.path = ?", (entry["path"],)).fetchall()
            for request_id, serialized in rows:
                result = json.loads(serialized)
                result.update({"commit": commit, "pending": False})
                db.execute("UPDATE receipts SET result = ? WHERE request_id = ?", (json.dumps(result), request_id))
            db.execute("DELETE FROM pending_requests WHERE path = ?", (entry["path"],))
            db.execute("DELETE FROM pending WHERE path = ?", (entry["path"],))


def recover_transaction(store, txn):
    if txn["kind"] == "pending_write":
        path = store._path(txn["path"])
        old = None if txn["old"] is None else base64.b64decode(txn["old"])
        new = base64.b64decode(txn["new"])
        if store._ref() != txn["ref"] or store._head() != txn["old_head"] or store._read(path) not in (old, new):
            raise VaultError("RECOVERY_CONFLICT", "External changes conflict with the deferred write.")
        if store._pending_entry(txn["path"]) not in (txn["previous_pending"], txn["pending"]):
            raise VaultError("RECOVERY_CONFLICT", "Pending write metadata changed outside VaultWrite.")
        if store._read(path) != new:
            store._atomic_write(path, new, txn["file_mode"])
        _save_write(store, txn)
        store._clear_journal()
        return
    head = store._head()
    if store._ref() != txn["ref"] or head not in (txn["old_head"], txn["new_head"]):
        raise VaultError("RECOVERY_CONFLICT", "The branch changed during a pending batch commit.")
    for entry in txn["entries"]:
        if version(store._read(store._path(entry["path"]))) != entry["version"]:
            raise VaultError("RECOVERY_CONFLICT", "A note changed during a pending batch commit.")
        expected = {key: value for key, value in entry.items() if key != "blob"}
        pending = store._pending_entry(entry["path"])
        if pending != expected and not (pending is None and head == txn["new_head"]):
            raise VaultError("RECOVERY_CONFLICT", "Pending batch metadata changed outside VaultWrite.")
        _check_index(store, entry)
    if head == txn["old_head"]:
        store._git("update-ref", txn["ref"], txn["new_head"], txn["old_head"])
    for entry in txn["entries"]:
        store._git("update-index", "--add", "--cacheinfo", entry["git_mode"], entry["blob"], entry["path"])
    _finish_batch(store, txn["entries"], txn["new_head"])
    store._clear_journal()


def flush(store):
    with store._locked():
        store._recover()
        entries = store._pending_entries()
        if not entries:
            return {"changed": False, "commit": store._head(), "files": []}
        head, ref = store._head(), store._ref()
        if store._git("diff", "--cached", "--name-only", "-z", head):
            raise VaultError("STAGED_CHANGES", "Unstage external changes before flushing the batch.")
        fd, temporary = tempfile.mkstemp(prefix="index-", dir=store.state)
        os.close(fd)
        index = Path(temporary)
        index.unlink()
        try:
            store._git("read-tree", head, index=index)
            for entry in entries:
                content = store._read(store._path(entry["path"]))
                baseline = store._git("ls-tree", "-z", head, "--", entry["path"])
                old_blob = baseline.split(b"\t", 1)[0].decode().split()[2] if baseline else None
                if entry["ref"] != ref or old_blob != entry["old_blob"] or version(content) != entry["version"]:
                    raise VaultError("DIRTY_TARGET", "The note or its Git baseline changed outside VaultWrite.")
                entry["blob"] = store._git("hash-object", "-w", "--stdin", data=content).decode().strip()
                store._git("update-index", "--add", "--cacheinfo", entry["git_mode"], entry["blob"], entry["path"], index=index)
            tree = store._git("write-tree", index=index).decode().strip()
            if tree == store._git("rev-parse", head + "^{tree}").decode().strip():
                _finish_batch(store, entries, head)
                return {"changed": False, "commit": head, "files": [entry["path"] for entry in entries]}
            fallback = f"docs: 更新 {len(entries)} 篇知识笔记"
            diff = ""
            if store.settings.model.enabled:
                diff = store._git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", head, index=index).decode(errors="replace")
            subject = store._subject(diff, fallback)
            with store._db() as db:
                request_ids = [row[0] for row in db.execute("SELECT request_id FROM pending_requests ORDER BY request_id")]
            actors = sorted({actor for entry in entries for actor in entry["actors"]})
            message = subject + "\n\nVaultWrite-Actors: " + ", ".join(actors) + "\nVaultWrite-Requests:\n" + "".join(f"- {request_id}\n" for request_id in request_ids)
            commit = store._git("commit-tree", tree, "-p", head, data=message.encode()).decode().strip()
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)
        txn = {"kind": "batch_commit", "ref": ref, "old_head": head, "new_head": commit, "entries": entries}
        store._atomic_write(store.journal, json.dumps(txn).encode())
        store._checkpoint("prepared")
        try:
            store._git("update-ref", ref, commit, head)
            store._checkpoint("ref_updated")
            for entry in entries:
                store._git("update-index", "--add", "--cacheinfo", entry["git_mode"], entry["blob"], entry["path"])
            _finish_batch(store, entries, commit)
            store._checkpoint("receipt_saved")
            store._clear_journal()
        except Exception as exc:
            raise VaultError("RECOVERY_REQUIRED", "The batch commit is pending recovery; resolve the Git/filesystem error and retry flush.") from exc
        return {"changed": True, "commit": commit, "files": [entry["path"] for entry in entries]}


def push(store):
    config = store.settings.push
    if not config.enabled:
        return {"pushed": False}
    with store._locked():
        store._recover()
        head, ref = store._head(), store._ref()
        branch = config.branch or ref.removeprefix("refs/heads/")
        store._git("check-ref-format", "refs/heads/" + branch)
        checkpoint = {"head": head, "ref": ref, "remote": config.remote, "branch": branch}
        path = store.state / "push.json"
        if path.exists() and json.loads(path.read_text()) == checkpoint:
            return {"pushed": False, "commit": head}
        store._git("push", "--porcelain", "--", config.remote, head + ":refs/heads/" + branch)
        store._atomic_write(path, json.dumps(checkpoint).encode())
        return {"pushed": True, "commit": head}
