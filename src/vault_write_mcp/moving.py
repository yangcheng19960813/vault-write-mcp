"""Previewed, version-checked note moves with one recoverable multi-file commit.

Only the service lock hides partial application: external readers can observe
individual replacements. External writers must remain stopped during ownership.
"""

import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import tempfile
import unicodedata
from pathlib import Path

from .store import VaultError, _sync_directory, version
from .sync import _clean, _path, _tree, validate_branch


MAX_SCAN_FILES = 2000
MAX_SCAN_NOTES = 1000
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_AFFECTED_FILES = 100
MAX_SCAN_LINKS = 20000
PLAN_FORMAT = "vault-move-v1"


def _visible_markdown(name):
    return name.endswith(".md") and all(not part.startswith(".") for part in name.split("/"))


def _entry_path(store, name):
    path = store._path(name)
    _path(store, name)
    return path


def _move_path(store, name):
    path = _entry_path(store, name)
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise VaultError("INVALID_PATH", "Move paths must be valid UTF-8.") from exc
    if any(char in name for char in "#|^:%[]()<>?`"):
        raise VaultError("INVALID_PATH", "Move paths must not contain link delimiters (# | ^ : % [ ] ( ) < > ? `).")
    _path(store, name)
    return path


def _canonical(name):
    return unicodedata.normalize("NFC", name).casefold().rstrip(". ")


def _topology(names):
    prefixes = {}
    files = set(names)
    for name in sorted(files):
        parts = name.split("/")
        if PurePosixPath(name).is_absolute() or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise VaultError("UNSUPPORTED_TREE", "Move catalogs require safe UTF-8 Git paths.")
        if any(part in ("", ".", "..") or _canonical(part) == ".git" for part in parts):
            raise VaultError("UNSUPPORTED_TREE", "Unsafe path in move catalog.")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = "/".join(_canonical(part) for part in parts[:length])
            if key in prefixes and prefixes[key] != prefix:
                raise VaultError("PATH_COLLISION", "Paths collide under case or Unicode normalization.", path=name)
            prefixes[key] = prefix
            if length < len(parts) and prefix in files:
                raise VaultError("PATH_COLLISION", "A file obstructs the destination directory.", path=name)


def _untracked(store):
    ordinary = store._git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    ignored = store._git("ls-files", "--others", "--ignored", "--exclude-standard", "-z").decode().split("\0")
    return {name for name in ordinary if name} | {name for name in ignored if name and _visible_markdown(name)}


def _attributes(store, names):
    for name in names:
        attributes = store._git("check-attr", "-z", "filter", "working-tree-encoding", "--", name).split(b"\0")
        if attributes[2::3] != [b"unspecified", b"unspecified"]:
            raise VaultError("UNSUPPORTED_ATTRIBUTES", "Move files cannot use Git filters or working-tree-encoding.", path=name)


def _plan(store, source, destination):
    from .links import rewrite_note_links
    src, dst = _move_path(store, source), _move_path(store, destination)
    store._check_write_paths([source, destination])
    if source == destination or _canonical(source) == _canonical(destination):
        raise VaultError("PATH_COLLISION", "Source and destination must be distinct, including case and Unicode normalization.")
    if store.settings.sync.enabled:
        validate_branch(store)
    if store._pending_entries():
        raise VaultError("PENDING_BATCH", "Flush pending interval writes or wait for the worker before previewing/moving; moves never flush implicitly.")
    _clean(store)
    if _untracked(store):
        raise VaultError("EXTERNAL_CHANGES", "Commit or remove untracked and ignored visible Markdown before moving.")
    head, ref = store._head(), store._ref()
    tree = _tree(store, head)
    if len(tree) > MAX_SCAN_FILES:
        raise VaultError("MOVE_TOO_LARGE", f"Moves scan at most {MAX_SCAN_FILES} tracked files.")
    if source not in tree or not src.exists():
        raise VaultError("NOT_FOUND", "The source must be a committed visible Markdown note.")
    if destination in tree or dst.exists():
        raise VaultError("DESTINATION_EXISTS", "The destination must be absent.")
    _topology(set(tree) | {destination})
    catalog = set()
    for name, (mode, kind, _) in tree.items():
        if kind != "blob" or mode not in ("100644", "100755"):
            raise VaultError("UNSUPPORTED_TREE", "Moves support regular tracked files only.", path=name)
        catalog.add(name)
    names = sorted(name for name in tree if _visible_markdown(name))
    if len(names) > MAX_SCAN_NOTES:
        raise VaultError("MOVE_TOO_LARGE", f"Moves scan at most {MAX_SCAN_NOTES} visible Markdown notes.")
    notes, modes, total = {}, {}, 0
    for name in names:
        target = store._path(name)
        _path(store, name)
        data = store._read(target)
        if data is None or store._git("cat-file", "blob", tree[name][2]) != data:
            raise VaultError("EXTERNAL_CHANGES", "A tracked note differs from its committed snapshot.", path=name)
        total += len(data)
        if total > MAX_SCAN_BYTES:
            raise VaultError("MOVE_TOO_LARGE", "Moves scan at most 16 MiB of note content.")
        notes[name] = data
        modes[name] = stat.S_IMODE(target.stat().st_mode)
    rewritten, count = {}, 0
    link_budget = [MAX_SCAN_LINKS]
    target_summary = {"items": [], "remaining_bytes": 16384, "truncated": False}
    for name, data in notes.items():
        new, changes = rewrite_note_links(data, name, destination if name == source else name, source, destination, catalog, link_budget=link_budget, target_summary=target_summary)
        if new != data:
            if len(new) > store.MAX_BYTES:
                raise VaultError("TOO_LARGE", "A rewritten note would exceed 1 MiB.", path=name)
            rewritten[name] = new
        count += changes
    new_source = rewritten.pop(source, notes[source])
    expected = {source: version(notes[source]), destination: "missing"}
    entries = [
        {"path": source, "old": notes[source], "new": None, "file_mode": modes[source], "git_mode": tree[source][0]},
        {"path": destination, "old": None, "new": new_source, "file_mode": modes[source], "git_mode": tree[source][0]},
    ]
    for name, new in sorted(rewritten.items()):
        expected[name] = version(notes[name])
        entries.append({"path": name, "old": notes[name], "new": new, "file_mode": modes[name], "git_mode": tree[name][0]})
    if len(entries) > MAX_AFFECTED_FILES:
        raise VaultError("MOVE_TOO_LARGE", f"Moves change at most {MAX_AFFECTED_FILES} paths.")
    store._check_write_paths([entry["path"] for entry in entries])
    for entry in entries:
        if entry["new"] is not None:
            store._check_atomic_device(store._path(entry["path"]).parent)
    _attributes(store, [entry["path"] for entry in entries])
    signature = [PLAN_FORMAT, source, destination, head, ref, sorted(tree.items()),
                 [(name, version(data), modes[name]) for name, data in notes.items()]]
    plan_version = version(json.dumps(signature, ensure_ascii=True, sort_keys=True).encode())
    public = {"source": source, "destination": destination, "plan_version": plan_version,
              "expected_versions": expected, "affected_paths": sorted(expected), "rewritten_links": count,
              "scan_count": len(notes), "link_changes": target_summary["items"],
              "link_changes_truncated": target_summary["truncated"], "versions_after": {entry["path"]: version(entry["new"]) for entry in entries}}
    return {"public": public, "entries": entries, "head": head, "ref": ref,
            "note_versions": {name: version(data) for name, data in notes.items()}, "note_modes": modes}


def preview_move_note(store, source, destination):
    with store._locked():
        store._check_write_paths([source, destination])
        if (store.settings.write_policy.allow_paths or store.settings.write_policy.deny_paths) and not store.journal.exists():
            _plan(store, source, destination)  # Read-only full policy preflight before cleanup.
        store._recover()
        return _plan(store, source, destination)["public"]


def _request_arguments(expected_versions, plan_version, request_id, actor):
    if not isinstance(expected_versions, dict) or not expected_versions or len(expected_versions) > MAX_AFFECTED_FILES:
        raise VaultError("INVALID_VERSION", "expected_versions must be the exact nonempty mapping returned by preview_move_note.")
    if any(not isinstance(name, str) or not isinstance(value, str) or not re.fullmatch(r"missing|sha256:[0-9a-f]{64}", value) for name, value in expected_versions.items()):
        raise VaultError("INVALID_VERSION", "Expected versions must be whole-note SHA-256 values or missing.")
    if not isinstance(plan_version, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", plan_version):
        raise VaultError("INVALID_VERSION", "Copy plan_version from preview_move_note.")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise VaultError("INVALID_REQUEST_ID", "request_id must contain 1–128 ASCII letters, digits, or _ . : -.")
    if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,64}", actor):
        raise VaultError("INVALID_ACTOR", "actor must contain 1–64 ASCII letters, digits, or _ . @ : -.")


def _prepare_commit(store, plan, source, destination, request_id, actor):
    fd, temporary = tempfile.mkstemp(prefix="index-", dir=store.state)
    os.close(fd)
    index = Path(temporary)
    index.unlink()
    try:
        store._git("read-tree", plan["head"], index=index)
        for entry in plan["entries"]:
            if entry["new"] is None:
                store._git("update-index", "--force-remove", "--", entry["path"], index=index)
            else:
                blob = store._git("hash-object", "-w", "--stdin", data=entry["new"]).decode().strip()
                store._git("update-index", "--add", "--cacheinfo", entry["git_mode"], blob, entry["path"], index=index)
        tree = store._git("write-tree", index=index).decode().strip()
        subject = f"Move {source} to {destination}"
        if store.settings.model.enabled:
            diff = store._git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", plan["head"], index=index).decode(errors="replace")
            subject = store._subject(diff, subject)
        message = f"{subject}\n\nVaultWrite-Actor: {actor}\nVaultWrite-Request: {request_id}\n"
        commit = store._git("commit-tree", tree, "-p", plan["head"], data=message.encode()).decode().strip()
        return commit
    finally:
        index.unlink(missing_ok=True)
        Path(str(index) + ".lock").unlink(missing_ok=True)


def _verify_move(store, txn):
    try:
        _verify_move_snapshot(store, txn)
    except VaultError as exc:
        if exc.code in {"INVALID_PATH", "EXTERNAL_CHANGES", "UNSUPPORTED_TREE", "UNSUPPORTED_FILESYSTEM"}:
            raise VaultError("RECOVERY_CONFLICT", "A move path or filesystem changed. Preserve pending.json for inspection.", reason_code=exc.code) from exc
        raise
    except OSError as exc:
        raise VaultError("RECOVERY_CONFLICT", "A move file cannot be inspected safely. Preserve pending.json for inspection.") from exc


def _verify_move_snapshot(store, txn):
    def conflict(message):
        return VaultError("RECOVERY_CONFLICT", message + " Preserve pending.json for inspection.")
    if store._ref() != txn["ref"] or store._head() not in (txn["old_head"], txn["new_head"]) or store._pending_entries():
        raise conflict("The branch or pending batch changed during a move.")
    allowed_trees = {store._git("rev-parse", head + "^{tree}").decode().strip() for head in (txn["old_head"], txn["new_head"])}
    if store._git("write-tree").decode().strip() not in allowed_trees:
        raise conflict("The index changed during a move.")
    affected = {entry["path"] for entry in txn["entries"]}
    if _untracked(store) - affected:
        raise conflict("Untracked or ignored Markdown paths appeared during a move.")
    dirty = set(store._git("diff", "--name-only", "--no-ext-diff", "--no-textconv", "-z").decode().split("\0")) - {""}
    if dirty - affected:
        raise conflict("Unrelated tracked files changed during a move.")
    for name, expected in txn["note_versions"].items():
        if name not in affected:
            path = _path(store, name)
            if version(store._read(path)) != expected or stat.S_IMODE(path.stat().st_mode) != txn["note_modes"][name]:
                raise conflict("An unchanged note was modified during a move.")
    # Validate every entry before writing any one entry; no partial overwrite on
    # a later conflict, symlink, changed permission or different mount.
    for entry in txn["entries"]:
        path = _entry_path(store, entry["path"])
        current = store._read(path)
        options = [None if entry[key] is None else base64.b64decode(entry[key]) for key in ("old", "new")]
        if current not in options:
            raise conflict("A move file changed outside VaultWrite.")
        if current is not None and stat.S_IMODE(path.stat().st_mode) != entry["file_mode"]:
            raise conflict("A move file's permissions changed outside VaultWrite.")
        if entry["new"] is not None:
            store._check_atomic_device(path.parent)


def recover_move(store, txn):
    _verify_move(store, txn)
    for entry in txn["entries"]:
        if entry["new"] is not None:
            path = _entry_path(store, entry["path"])
            data = base64.b64decode(entry["new"])
            if store._read(path) != data:
                store._atomic_write(path, data, entry["file_mode"])
            store._checkpoint("move_file_written:" + entry["path"])
    for entry in txn["entries"]:
        if entry["new"] is None:
            path = _entry_path(store, entry["path"])
            if path.exists():
                path.unlink()
                _sync_directory(path.parent)
            store._checkpoint("move_source_removed")
    if store._head() == txn["old_head"]:
        store._git("update-ref", txn["ref"], txn["new_head"], txn["old_head"])
    store._checkpoint("move_ref_updated")
    store._git("read-tree", txn["new_head"])
    store._checkpoint("move_index_updated")
    store._save_receipt(txn["request_id"], txn["fingerprint"], txn["result"])
    store._checkpoint("move_receipt_saved")
    store._clear_journal()


def move_note(store, source, destination, expected_versions, plan_version, request_id, actor):
    _request_arguments(expected_versions, plan_version, request_id, actor)
    arguments = ["move", source, destination, expected_versions, plan_version, actor]
    fingerprint = hashlib.sha256(json.dumps(arguments, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    with store._locked():
        store._check_write_paths([source, destination])
        # Also guard all caller-supplied paths before recovery. A completed
        # receipt has its own authoritative affected set, checked below.
        store._check_write_paths(expected_versions)
        previous = store._receipt(request_id, fingerprint)
        if previous is not None:
            store._check_write_paths(previous["affected_paths"])
        elif (store.settings.write_policy.allow_paths or store.settings.write_policy.deny_paths) and not store.journal.exists():
            _plan(store, source, destination)  # Guard omitted backlink paths before cleanup.
        store._recover()
        previous = store._receipt(request_id, fingerprint)
        if previous is not None:
            store._check_write_paths(previous["affected_paths"])
            return previous
        plan = _plan(store, source, destination)
        if expected_versions != plan["public"]["expected_versions"]:
            raise VaultError("VERSION_CONFLICT", "The expected_versions mapping changed; preview again.")
        if plan_version != plan["public"]["plan_version"]:
            raise VaultError("PLAN_CONFLICT", "The vault topology or snapshot changed; preview again.")
        commit = _prepare_commit(store, plan, source, destination, request_id, actor)
        result = {"source": source, "destination": destination, "versions": plan["public"]["versions_after"], "commit": commit,
                  "request_id": request_id, "changed": True, "replayed": False, "rewritten_links": plan["public"]["rewritten_links"],
                  "affected_paths": plan["public"]["affected_paths"]}
        entries = [{**entry, "old": None if entry["old"] is None else base64.b64encode(entry["old"]).decode(),
                    "new": None if entry["new"] is None else base64.b64encode(entry["new"]).decode()} for entry in plan["entries"]]
        txn = {"kind": "move_note", "old_head": plan["head"], "new_head": commit, "ref": plan["ref"], "entries": entries,
               "note_versions": plan["note_versions"], "note_modes": plan["note_modes"],
               "request_id": request_id, "fingerprint": fingerprint, "result": result}
        store._atomic_write(store.journal, json.dumps(txn).encode())
        store._checkpoint("move_prepared")
        try:
            recover_move(store, txn)
        except Exception as exc:
            raise VaultError("RECOVERY_REQUIRED", "The move is pending recovery; retry the identical request after resolving the filesystem/Git error.", reason=str(exc)) from exc
        return result
