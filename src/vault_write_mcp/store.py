"""One repository lock covers version checking, file replacement and Git history.

The durable journal bridges the filesystem, Git ref/index and receipt database.
All VaultWrite processes must use the same local repository; external writers
must be stopped while the service owns it.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import tempfile
from typing import Iterator

from .config import Settings
from .messages import CommitMessages, MessageError


class VaultError(Exception):
    def __init__(self, code: str, message: str, **details: object):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def version(data: bytes | None) -> str:
    return "missing" if data is None else "sha256:" + hashlib.sha256(data).hexdigest()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class VaultStore:
    MAX_BYTES = 1024 * 1024

    def __init__(self, root: str | Path, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.messages = CommitMessages(self.settings.model)
        self.root = Path(root).expanduser().resolve(strict=True)
        git_dir = self.root / ".git"
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise VaultError("INVALID_VAULT", "Use a regular Git repository, not a linked worktree or bare repository.")
        if Path(self._git("rev-parse", "--show-toplevel").decode().strip()).resolve() != self.root:
            raise VaultError("INVALID_VAULT", "The vault must be the repository root.")
        self.state = git_dir / "vault-write"
        if self.state.is_symlink():
            raise VaultError("INVALID_VAULT", "The state directory must not be a symlink.")
        self.journal = self.state / "pending.json"
        self.database = self.state / "receipts.sqlite3"
        # A stricter policy must stop an old journal before schema/lock creation
        # or atomic-file cleanup, not after partially completing recovery.
        self._preflight_recovery_policy()
        self.state.mkdir(mode=0o700, exist_ok=True)
        with self._locked():
            with self._db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS receipts (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS pending (path TEXT PRIMARY KEY, state TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS pending_requests (request_id TEXT PRIMARY KEY, path TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS atomic_files (name TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL)")
            self._ref()
            self._head()
            self._recover()
            if self.settings.sync.enabled:
                from .sync import validate_branch
                validate_branch(self)

    def _git(self, *args: str, **kwargs) -> bytes:
        return self._git_run(*args, **kwargs).stdout

    def _git_run(self, *args: str, data: bytes | None = None, index: Path | None = None,
                 cwd: Path | None = None, allowed_codes: tuple[int, ...] = (0,),
                 operator_config: bool = False) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({
            "GIT_AUTHOR_NAME": "VaultWrite", "GIT_AUTHOR_EMAIL": "vaultwrite@localhost",
            "GIT_COMMITTER_NAME": "VaultWrite", "GIT_COMMITTER_EMAIL": "vaultwrite@localhost",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        if (operator_config or (args and args[0] in ("push", "fetch"))) and cwd is None:
            # Git authentication and URL aliases often live in the user's config.
            env.pop("GIT_CONFIG_GLOBAL")
            for name in ("GIT_CONFIG_GLOBAL", "GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT"):
                if name in os.environ:
                    env[name] = os.environ[name]
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        try:
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                 "--literal-pathspecs", *args],
                cwd=cwd or self.root, env=env, input=data, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VaultError("GIT_ERROR", str(exc)) from exc
        if result.returncode not in allowed_codes:
            raise VaultError("GIT_ERROR", result.stderr.decode(errors="replace").strip() or "Git command failed.")
        return result

    def _head(self) -> str:
        return self._git("rev-parse", "--verify", "HEAD").decode().strip()

    def _ref(self) -> str:
        try:
            return self._git("symbolic-ref", "-q", "HEAD").decode().strip()
        except VaultError as exc:
            raise VaultError("INVALID_VAULT", "A named branch with an initial commit is required.") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(self.state / "write.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise VaultError("INVALID_PATH", "Use a relative POSIX Markdown path without control characters.")
        parts = name.split("/")
        if any(not part or part.startswith(".") for part in parts) or PurePosixPath(name).is_absolute() or not name.endswith(".md"):
            raise VaultError("INVALID_PATH", "Only .md files in visible vault directories are supported.")
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise VaultError("INVALID_PATH", "Symlinks are not supported.")
            if current != self.root and current.is_dir() and (current / ".git").exists():
                raise VaultError("INVALID_PATH", "Nested repositories are not supported.")
        if current.exists() and not current.is_file():
            raise VaultError("INVALID_PATH", "The target must be a regular file.")
        return current

    def _check_write_paths(self, names) -> None:
        if not (self.settings.write_policy.allow_paths or self.settings.write_policy.deny_paths):
            return
        from .policy import permits
        # Check the complete affected set before the caller changes anything.
        for name in names:
            self._path(name)
            if not permits(self.settings.write_policy, name):
                raise VaultError("PATH_PROTECTED", "The current write policy protects an affected path; no new operation was applied. For blocked recovery, preserve state and restore an allowing policy before restarting.", path=name)

    def _preflight_recovery_policy(self) -> None:
        if not (self.settings.write_policy.allow_paths or self.settings.write_policy.deny_paths):
            return
        try:
            fd = os.open(self.journal, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        except OSError:
            raise VaultError("RECOVERY_REQUIRED", "Cannot inspect pending journal safely; preserve state and run doctor.") from None
        try:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
                    raise ValueError("Invalid journal")
                txn = json.load(stream)
            if not isinstance(txn, dict):
                raise ValueError("Invalid journal")
            kind = txn.get("kind")
            if kind == "sync_checkout":
                # These rules protect explicit write APIs, not Git synchronization.
                return
            if kind in ("move_note", "batch_commit"):
                entries = txn["entries"]
                if not isinstance(entries, list) or not entries:
                    raise ValueError("Invalid journal entries")
                names = [entry["path"] for entry in entries]
            elif kind in (None, "pending_write"):
                names = [txn["path"]]
            else:
                raise ValueError("Unknown journal")
        except (ValueError, KeyError, TypeError, UnicodeError, OSError, RecursionError):
            raise VaultError("RECOVERY_REQUIRED", "Cannot inspect pending journal safely; preserve state and run doctor.") from None
        self._check_write_paths(names)

    def _read(self, path: Path) -> bytes | None:
        try:
            with path.open("rb") as stream:
                data = stream.read(self.MAX_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(data) > self.MAX_BYTES:
            raise VaultError("TOO_LARGE", "Notes must be at most 1 MiB.")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VaultError("INVALID_ENCODING", "Notes must be UTF-8.") from exc
        return data

    def _ensure_parent(self, parent: Path) -> None:
        current = self.root
        for part in parent.relative_to(self.root).parts:
            next_path = current / part
            if not next_path.exists():
                next_path.mkdir()
                _sync_directory(current)
            current = next_path

    def _check_atomic_device(self, parent: Path) -> None:
        # A private staging file can only be renamed atomically on the same
        # filesystem. Never fall back to a non-atomic copy across a mount.
        existing = parent
        while not existing.exists():
            existing = existing.parent
        if existing.stat().st_dev != self.state.stat().st_dev:
            raise VaultError("UNSUPPORTED_FILESYSTEM", "Managed notes and .git/vault-write must share a filesystem for atomic replacement.")

    def _cleanup_atomic_files(self) -> None:
        # An exact durable (name, device, inode) receipt is ownership evidence;
        # neither a filename prefix nor a directory glob is sufficient.
        with self._db() as db:
            rows = db.execute("SELECT name, device, inode FROM atomic_files").fetchall()
            for name, device, inode in rows:
                if not re.fullmatch(r"atomic-[A-Za-z0-9_\-]+\.tmp", name):
                    continue
                temporary = self.state / name
                try:
                    info = temporary.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISREG(info.st_mode) and info.st_dev == device and info.st_ino == inode and info.st_nlink == 1:
                        temporary.unlink()
                        _sync_directory(self.state)
                    # A replaced path, hard link or symlink is not ours to
                    # remove. Retain it, but don't block normal vault work.
                db.execute("DELETE FROM atomic_files WHERE name = ?", (name,))

    def _atomic_write(self, path: Path, data: bytes, mode: int = 0o600) -> None:
        self._check_atomic_device(path.parent)
        self._ensure_parent(path.parent)
        # Staging under .git means even a kill between creation and ownership
        # registration cannot leak an untracked Markdown-directory tempfile
        # into a later Git sync. Such unregistered private files are retained.
        fd, temporary = tempfile.mkstemp(prefix="atomic-", suffix=".tmp", dir=self.state)
        name = Path(temporary).name
        owned = os.fstat(fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                self._checkpoint("atomic_created")
                info = os.fstat(stream.fileno())
                with self._db() as db:
                    db.execute("INSERT INTO atomic_files VALUES (?, ?, ?)", (name, info.st_dev, info.st_ino))
                _sync_directory(self.state)
                self._checkpoint("atomic_registered")
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                self._checkpoint("atomic_fsynced")
            os.replace(temporary, path)
            _sync_directory(path.parent)
            if path.parent != self.state:
                _sync_directory(self.state)
            self._checkpoint("atomic_replaced")
        finally:
            # The live process still owns this exact pathname. Recovery instead
            # uses the durable inode receipt and never guesses by prefix.
            try:
                remaining = os.lstat(temporary)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISREG(remaining.st_mode) and (remaining.st_dev, remaining.st_ino) == (owned.st_dev, owned.st_ino) and remaining.st_nlink == 1:
                    os.unlink(temporary)
                    _sync_directory(self.state)
            with self._db() as db:
                db.execute("DELETE FROM atomic_files WHERE name = ?", (name,))

    def _clear_journal(self) -> None:
        self.journal.unlink()
        _sync_directory(self.state)

    def _receipt(self, request_id: str, fingerprint: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT fingerprint, result FROM receipts WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise VaultError("IDEMPOTENCY_CONFLICT", "This request_id was already used with different arguments.")
        return {**json.loads(row[1]), "replayed": True}

    def _save_receipt(self, request_id: str, fingerprint: str, result: dict) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?)", (request_id, fingerprint, json.dumps(result)))
            if result.get("pending"):
                db.execute("INSERT OR IGNORE INTO pending_requests VALUES (?, ?)", (request_id, result["path"]))

    def _pending_entries(self) -> list[dict]:
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT state FROM pending ORDER BY path")]

    def _pending_entry(self, path: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT state FROM pending WHERE path = ?", (path,)).fetchone()
        return json.loads(row[0]) if row else None

    def _subject(self, diff: str, fallback: str) -> str:
        try:
            return self.messages.generate(diff, fallback)
        except MessageError as exc:
            raise VaultError("MODEL_ERROR", str(exc)) from exc

    def flush(self) -> dict:
        from .batch import flush
        return flush(self)

    def push(self) -> dict:
        if self.settings.sync.enabled:
            return self.sync()
        from .batch import push
        return push(self)

    def sync(self) -> dict:
        from .sync import synchronize
        return synchronize(self)

    def sync_status(self) -> dict:
        from .sync import status
        return status(self)

    def _checkpoint(self, stage: str) -> None:
        """Overridden by failure-injection tests; no runtime bypass or failpoint."""

    def _recover(self) -> None:
        self._preflight_recovery_policy()
        self._cleanup_atomic_files()
        if not self.journal.exists():
            return
        txn = json.loads(self.journal.read_text(encoding="utf-8"))
        if txn.get("kind") == "move_note":
            from .moving import recover_move
            recover_move(self, txn)
            return
        if txn.get("kind") == "sync_checkout":
            from .sync import recover_checkout
            recover_checkout(self, txn)
            return
        if txn.get("kind") in ("pending_write", "batch_commit"):
            from .batch import recover_transaction
            recover_transaction(self, txn)
            return
        path = self._path(txn["path"])
        old = None if txn["old"] is None else base64.b64decode(txn["old"])
        new = base64.b64decode(txn["new"])
        current = self._read(path)
        head = self._head()
        if self._ref() != txn["ref"] or head not in (txn["old_head"], txn["new_head"]) or current not in (old, new):
            raise VaultError("RECOVERY_CONFLICT", "Pending transaction conflicts with external changes; preserve pending.json and inspect the vault.")
        # Do not overwrite an operator's independently staged version of this note.
        staged = self._git("ls-files", "--stage", "-z", "--", txn["path"])
        if staged:
            metadata = staged.split(b"\t", 1)[0].decode().split()
            allowed_blobs = {txn["blob"], txn["old_blob"]}
            if len(staged.split(b"\0")) != 2 or metadata[0] != txn["git_mode"] or metadata[1] not in allowed_blobs or metadata[2] != "0":
                raise VaultError("RECOVERY_CONFLICT", "The target index entry changed outside VaultWrite.")
        elif txn["old_blob"] is not None:
            raise VaultError("RECOVERY_CONFLICT", "The target index entry was removed outside VaultWrite.")
        if current != new:
            self._atomic_write(path, new, txn["file_mode"])
        if head == txn["old_head"]:
            self._git("update-ref", txn["ref"], txn["new_head"], txn["old_head"])
        self._git("update-index", "--add", "--cacheinfo", txn["git_mode"], txn["blob"], txn["path"])
        self._save_receipt(txn["request_id"], txn["fingerprint"], txn["result"])
        self._clear_journal()

    def read_note(self, path: str) -> dict:
        with self._locked():
            self._recover()
            data = self._read(self._path(path))
            return {"path": path, "exists": data is not None, "content": None if data is None else data.decode("utf-8"), "version": version(data)}

    def read_note_range(self, path: str, start_line: int = 1, max_lines: int = 80) -> dict:
        from .editing import read_range
        with self._locked():
            self._recover()
            data = self._read(self._path(path))
            return {"path": path, "exists": data is not None, "version": version(data), **read_range(data, start_line, max_lines)}

    def find_note(self, path: str, text: str, start_line: int = 1, max_matches: int = 10,
                  context_before: int = 0, context_after: int = 0, compact: bool = False) -> dict:
        from .editing import find_lines
        with self._locked():
            self._recover()
            data = self._read(self._path(path))
            return {"path": path, "exists": data is not None, "version": version(data), **find_lines(
                data, text, start_line, max_matches, context_before, context_after, compact,
            )}

    def preview_move_note(self, source: str, destination: str) -> dict:
        from .moving import preview_move_note
        return preview_move_note(self, source, destination)

    def move_note(self, source: str, destination: str, expected_versions: dict[str, str], plan_version: str,
                  request_id: str, actor: str) -> dict:
        from .moving import move_note
        return move_note(self, source, destination, expected_versions, plan_version, request_id, actor)

    def _prepare_commit(self, path: str, content: bytes, head: str, mode: str, actor: str, request_id: str, operation: str) -> tuple[str, str]:
        fd, temporary = tempfile.mkstemp(prefix="index-", dir=self.state)
        os.close(fd)
        index = Path(temporary)
        index.unlink()  # read-tree requires an absent or valid index, not an empty file.
        try:
            self._git("read-tree", head, index=index)
            blob = self._git("hash-object", "-w", "--stdin", data=content).decode().strip()
            self._git("update-index", "--add", "--cacheinfo", mode, blob, path, index=index)
            tree = self._git("write-tree", index=index).decode().strip()
            subject = f"{operation} {path}"
            if self.settings.model.enabled:
                diff = self._git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", head, "--", path, index=index).decode(errors="replace")
                subject = self._subject(diff, subject)
            message = f"{subject}\n\nVaultWrite-Actor: {actor}\nVaultWrite-Request: {request_id}\n"
            commit = self._git("commit-tree", tree, "-p", head, data=message.encode()).decode().strip()
            return blob, commit
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)

    def write_note(self, path: str, content: str, expected_version: str, request_id: str, actor: str) -> dict:
        return self._mutate("write", path, content, expected_version, request_id, actor)

    def append_note(self, path: str, content: str, expected_version: str, request_id: str, actor: str) -> dict:
        return self._mutate("append", path, content, expected_version, request_id, actor)

    def patch_note(self, path: str, edits: list[dict], expected_version: str, request_id: str, actor: str,
                   return_context: bool = False, context_before: int = 0, context_after: int = 0) -> dict:
        from .editing import validate_context, validate_edits
        if type(return_context) is not bool:
            raise VaultError("INVALID_CONTEXT", "return_context must be a boolean.")
        validate_context(context_before, context_after)
        if not return_context and (context_before or context_after):
            raise VaultError("INVALID_CONTEXT", "Set return_context=true when requesting patch context.")
        options = {"return_context": True, "context_before": context_before, "context_after": context_after} if return_context else None
        return self._mutate("patch", path, validate_edits(edits), expected_version, request_id, actor, options)

    def _mutate(self, operation: str, path: str, content: str | list[dict], expected_version: str, request_id: str, actor: str,
                result_options: dict | None = None) -> dict:
        if not isinstance(expected_version, str) or not re.fullmatch(r"missing|sha256:[0-9a-f]{64}", expected_version):
            raise VaultError("INVALID_VERSION", "expected_version must be 'missing' or a read_note SHA-256 version; bypasses are not supported.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            raise VaultError("INVALID_REQUEST_ID", "request_id must contain 1–128 ASCII letters, digits, or _ . : -.")
        if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,64}", actor):
            raise VaultError("INVALID_ACTOR", "actor must contain 1–64 ASCII letters, digits, or _ . @ : -.")
        if operation != "patch" and not isinstance(content, str):
            raise VaultError("INVALID_CONTENT", "content must be a UTF-8 string.")
        try:
            encoded = (json.dumps(content, ensure_ascii=False) if operation == "patch" else content).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise VaultError("INVALID_ENCODING", "content must be valid UTF-8.") from exc
        if len(encoded) > self.MAX_BYTES:
            raise VaultError("TOO_LARGE", "Notes must be at most 1 MiB.")
        arguments = [operation, path, content, expected_version, actor]
        # Keep the original default fingerprint, so receipts written by older
        # servers remain replayable. Nondefault result options are semantic
        # request arguments, not a view of whatever file exists on a retry.
        if result_options is not None:
            arguments.append(result_options)
        fingerprint = hashlib.sha256(json.dumps(arguments, ensure_ascii=True).encode()).hexdigest()
        with self._locked():
            self._path(path)
            self._check_write_paths([path])
            self._recover()
            if self.settings.sync.enabled:
                from .sync import validate_branch
                validate_branch(self)
            target = self._path(path)
            previous = self._receipt(request_id, fingerprint)
            if previous is not None:
                return previous
            self._check_atomic_device(target.parent)
            if self.settings.commit.mode == "immediate" and self._pending_entries():
                raise VaultError("PENDING_BATCH", "Flush pending interval writes before switching to immediate mode.")
            old = self._read(target)
            if version(old) != expected_version:
                raise VaultError("VERSION_CONFLICT", "Re-read the note, merge your change, and retry.", current_version=version(old))
            if operation in ("append", "patch") and old is None:
                raise VaultError("NOT_FOUND", "Create the note with write_note before appending or patching.")
            if operation == "patch":
                from .editing import apply_patch_with_spans
                new, spans = apply_patch_with_spans(old, content)
            else:
                new = (old or b"") + encoded if operation == "append" else encoded
            if len(new) > self.MAX_BYTES:
                raise VaultError("TOO_LARGE", "The resulting note exceeds 1 MiB.")
            head, ref = self._head(), self._ref()
            # The normal index must be clean; a private index builds this commit.
            if self._git("diff", "--cached", "--name-only", "-z", head):
                raise VaultError("STAGED_CHANGES", "Commit or unstage existing staged changes before writing.")
            entry = self._git("ls-tree", "-z", head, "--", path)
            old_blob = None
            git_mode = "100644"
            pending = self._pending_entry(path)
            if entry:
                metadata = entry.split(b"\t", 1)[0].decode().split()
                git_mode, kind, old_blob = metadata
                if kind != "blob" or git_mode not in ("100644", "100755"):
                    raise VaultError("INVALID_PATH", "Only regular Git files are supported.")
                committed = self._git("cat-file", "blob", old_blob)
                if committed != old and pending is None:
                    raise VaultError("DIRTY_TARGET", "The note differs from HEAD; commit external edits before writing.")
            elif old is not None and pending is None:
                raise VaultError("UNTRACKED_TARGET", "Commit this existing untracked note before writing.")
            if pending and (pending["ref"] != ref or pending["old_blob"] != old_blob or pending["version"] != version(old)):
                raise VaultError("DIRTY_TARGET", "The pending note or its Git baseline changed outside VaultWrite.")
            result = {"path": path, "version": version(new), "commit": head, "request_id": request_id, "changed": new != old, "replayed": False}
            if result_options is not None:
                from .editing import patch_context
                result["result_context"] = patch_context(new, spans, result_options["context_before"], result_options["context_after"])
            if self.settings.commit.mode == "interval":
                result.update({"pending": pending is not None, "commit": None if pending else head})
            if new == old:
                self._save_receipt(request_id, fingerprint, result)
                return result
            attributes = self._git("check-attr", "-z", "filter", "working-tree-encoding", "--", path).split(b"\0")
            if attributes[2::3] != [b"unspecified", b"unspecified"]:
                raise VaultError("UNSUPPORTED_ATTRIBUTES", "Git filters and working-tree-encoding are not supported for managed notes.")
            if self.settings.commit.mode == "interval":
                from .batch import defer_write
                return defer_write(self, path, old, new, head, ref, old_blob, git_mode, request_id, fingerprint, result, actor)
            blob, commit = self._prepare_commit(path, new, head, git_mode, actor, request_id, operation)
            result["commit"] = commit
            file_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
            txn = {"path": path, "old": None if old is None else base64.b64encode(old).decode(), "new": base64.b64encode(new).decode(),
                   "old_head": head, "new_head": commit, "ref": ref, "blob": blob, "old_blob": old_blob,
                   "git_mode": git_mode, "file_mode": file_mode, "request_id": request_id, "fingerprint": fingerprint, "result": result}
            self._atomic_write(self.journal, json.dumps(txn).encode())
            self._checkpoint("prepared")
            try:
                self._atomic_write(target, new, file_mode)
                self._checkpoint("file_written")
                self._git("update-ref", ref, commit, head)
                self._checkpoint("ref_updated")
                self._git("update-index", "--add", "--cacheinfo", git_mode, blob, path)
                self._save_receipt(request_id, fingerprint, result)
                self._checkpoint("receipt_saved")
                self._clear_journal()
            except Exception as exc:
                if self._head() == head and self._read(target) in (old, new):
                    if old is None:
                        target.unlink(missing_ok=True)
                        _sync_directory(target.parent)
                    else:
                        self._atomic_write(target, old, file_mode)
                    self._clear_journal()
                    raise VaultError("WRITE_FAILED", "The write failed and was rolled back; retry the same request_id.", reason=str(exc)) from exc
                try:
                    self._recover()
                except Exception as recovery_error:
                    raise VaultError("RECOVERY_REQUIRED", "A transaction is pending; retry the same request_id after resolving the Git/filesystem error.", reason=str(recovery_error)) from exc
            return result

    def note_history(self, path: str, limit: int = 20, follow_renames: bool = False) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise VaultError("INVALID_LIMIT", "limit must be between 1 and 100.")
        if type(follow_renames) is not bool:
            raise VaultError("INVALID_HISTORY", "follow_renames must be a boolean.")
        with self._locked():
            self._recover()
            self._path(path)
            options = ["--follow"] if follow_renames else []
            output = self._git("log", f"-{limit}", *options, "--format=%H%x00%at%x00%s", "--", path).decode()
            commits = []
            for line in output.splitlines():
                commit, timestamp, subject = line.split("\0", 2)
                commits.append({"commit": commit, "timestamp": int(timestamp), "subject": subject})
            result = {"path": path, "commits": commits}
            if follow_renames:
                result["rename_following"] = "git_similarity_heuristic"
            return result


def init_vault(root: str | Path) -> Path:
    """Create a new empty test vault. Never adopts an existing directory's files."""
    path = Path(root).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise VaultError("INVALID_VAULT", "init requires a new or empty directory.")
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    # Plumbing avoids hooks, signing configuration and modifications to user config.
    store = object.__new__(VaultStore)
    store.root = path
    tree = store._git("mktree", data=b"").decode().strip()
    commit = store._git("commit-tree", tree, data=b"Initialize VaultWrite vault\n").decode().strip()
    store._git("update-ref", "refs/heads/main", commit)
    return path
