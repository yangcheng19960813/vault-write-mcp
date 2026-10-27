"""Bounded, best-effort inspection. Never constructs a store or runs recovery.

Only fixed labels, counts and validated object IDs cross this boundary: damaged
state is untrusted and may contain credentials or private note text.
"""

import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
from urllib.parse import quote

from .config import Settings

MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_PATHS = 2000
MAX_COUNT = 100000
KINDS = {"pending_write", "batch_commit", "move_note", "sync_checkout"}
OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _file(path, limit):
    """Do not follow symlinks or block on a FIFO; no atime guarantee is made."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unsafe_or_unreadable", None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return "unsafe_or_unreadable", None
            if info.st_size > limit:
                return "too_large", None
            data = stream.read(limit + 1)
            return ("too_large", None) if len(data) > limit else ("present", data)
    except OSError:
        return "unsafe_or_unreadable", None


def _presence(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None  # Unknown is never evidence that a sidecar is absent.


def _directory(path):
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                                 "--literal-pathspecs", *args], cwd=root, env=env, capture_output=True, timeout=5)
        # Commands below return only an object ID/ref/root or one bool exit code.
        return (result.returncode, result.stdout[:8192])
    except (OSError, subprocess.TimeoutExpired):
        return (-1, b"")


def _counts(database):
    status, _ = _file(database, 128 * 1024 * 1024)
    if status != "present":
        return {"status": status}
    # immutable never creates/updates journal/WAL/SHM; intentionally only reads
    # the main DB; WAL or rollback state can make advisory counts incomplete.
    try:
        db = sqlite3.connect("file:" + quote(str(database), safe="/") + "?mode=ro&immutable=1", uri=True, timeout=0)
        try:
            remaining = [1000]
            def budget():
                remaining[0] -= 1
                return remaining[0] <= 0
            db.set_progress_handler(budget, 1000)
            counts = {}
            for table in ("receipts", "pending", "atomic_files"):
                counts[table] = db.execute(f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT {MAX_COUNT + 1})").fetchone()[0]
            return {"status": "readable", "snapshot": "immutable_main_database_only", "counts": counts,
                    "counts_capped": any(value > MAX_COUNT for value in counts.values())}
        finally:
            db.close()
    except (sqlite3.Error, OSError, ValueError):
        return {"status": "invalid_or_unsupported"}


def _journal(path, head, ref, settings):
    status, raw = _file(path, MAX_JOURNAL_BYTES)
    if status != "present":
        return {"status": status}
    try:
        txn = json.loads(raw)
        if not isinstance(txn, dict) or txn.get("kind") not in KINDS | {None}:
            raise ValueError("Unknown journal")
        kind = txn.get("kind") or "immediate_write"
        names = [txn["path"]] if kind in ("immediate_write", "pending_write") else [entry["path"] for entry in txn["entries"]]
        if not names or len(names) > MAX_DIAGNOSTIC_PATHS or any(not isinstance(name, str) for name in names):
            raise ValueError("Invalid paths")
        from .policy import permits
        for name in names:
            # Candidate paths are literal filenames, not rule syntax: brackets,
            # question marks and stars are legitimate existing POSIX names.
            if (len(name) > 16384 or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
                    or any(part in ("", ".", "..") or part.casefold() == ".git" for part in name.split("/"))):
                raise ValueError("Invalid path")
            if kind != "sync_checkout" and (not name.endswith(".md") or any(part.startswith(".") for part in name.split("/"))):
                raise ValueError("Invalid note path")
        old = txn.get("old_head")
        new = txn.get("new_head")
        if not isinstance(old, str) or not OID.fullmatch(old) or (kind != "pending_write" and (not isinstance(new, str) or not OID.fullmatch(new))):
            raise ValueError("Invalid heads")
        expected_ref = txn["ref"]
        if not isinstance(expected_ref, str) or not expected_ref.startswith("refs/heads/") or any(ord(c) < 32 for c in expected_ref):
            raise ValueError("Invalid ref")
        return {"status": "pending", "kind": kind, "affected_count": len(set(names)),
                "head_state": "old" if head == old else "new" if head == new else "other_or_unknown",
                "ref_matches": ref == expected_ref,
                "policy_allows": kind == "sync_checkout" or all(permits(settings.write_policy, name) for name in names)}
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        return {"status": "invalid_or_unsupported"}


def doctor(root, settings=None):
    """Inspect without locks/recovery. Output never includes arbitrary disk text."""
    result = {"read_only": True, "consistency": "best_effort_live_snapshot", "vault": {"status": "invalid_or_unreadable"},
              "recovery": {"state": "manual_inspection", "next_steps": ["Stop service and external writers; preserve the complete vault and .git before repair."]}}
    try:
        root = Path(root).expanduser().resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return result
    if not _directory(root / ".git"):
        return result
    code, top = _git(root, "rev-parse", "--show-toplevel")
    try:
        if code != 0 or Path(top.decode().strip()).resolve() != root:
            return result
    except (UnicodeError, OSError, ValueError):
        return result
    head_code, raw_head = _git(root, "rev-parse", "--verify", "HEAD")
    head = raw_head.decode("ascii", errors="replace").strip()
    head = head if head_code == 0 and OID.fullmatch(head) else None
    ref_code, raw_ref = _git(root, "symbolic-ref", "-q", "HEAD")
    ref = raw_ref.decode("utf-8", errors="replace").strip() if ref_code == 0 else None
    # diff --quiet avoids unbounded path/content output and optional index writes.
    working, _ = _git(root, "diff-files", "--quiet", "--no-ext-diff", "--no-textconv", "--ignore-submodules")
    staged, _ = _git(root, "diff-index", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", "--ignore-submodules", "HEAD")
    result["vault"] = {"status": "regular_git", "head": head, "named_branch": ref is not None,
                       "tracked_changes": "clean" if working == staged == 0 else "changed_or_stat_stale" if working == 1 or staged == 1 else "unknown"}
    state = root / ".git/vault-write"
    if _presence(state) is False:
        result.update(journal={"status": "absent"}, receipts={"status": "absent"})
    elif not _directory(state):
        result.update(journal={"status": "unsafe_or_unreadable"}, receipts={"status": "unsafe_or_unreadable"})
    else:
        result["journal"] = _journal(state / "pending.json", head, ref, settings or Settings())
        result["receipts"] = _counts(state / "receipts.sqlite3")
        for label, suffix in (("wal_present", "-wal"), ("rollback_journal_present", "-journal"), ("shm_present", "-shm")):
            result["receipts"][label] = _presence(state / ("receipts.sqlite3" + suffix))
        result["receipts"]["observation_complete"] = (result["receipts"]["status"] == "readable"
                                                       and all(result["receipts"][name] is False for name in ("wal_present", "rollback_journal_present", "shm_present")))
    journal = result["journal"]
    if journal["status"] == "absent":
        result["recovery"] = {"state": "no_journal", "next_steps": ["No transaction journal was observed; pending interval counts are advisory, not proof of publication."]}
    elif journal["status"] == "pending":
        state_label = "policy_blocked" if not journal["policy_allows"] else "pending_checks_required"
        result["recovery"] = {"state": state_label, "conditions": ["Recovery also requires matching file snapshots, index, branch and transaction metadata; this diagnostic does not prove recoverability."],
                              "next_steps": ["Stop writers and back up the complete vault and .git. Preserve pending.json and receipts.",
                                             "Restore an allowing policy before recovery if policy_blocked; resolve external changes manually, then restart with the original configuration and retry identical request arguments."]}
    return result
