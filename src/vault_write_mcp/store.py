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

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        git_dir = self.root / ".git"
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise VaultError("INVALID_VAULT", "Use a regular Git repository, not a linked worktree or bare repository.")
        if Path(self._git("rev-parse", "--show-toplevel").decode().strip()).resolve() != self.root:
            raise VaultError("INVALID_VAULT", "The vault must be the repository root.")
        self.state = git_dir / "vault-write"
        if self.state.is_symlink():
            raise VaultError("INVALID_VAULT", "The state directory must not be a symlink.")
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.journal = self.state / "pending.json"
        self.database = self.state / "receipts.sqlite3"
        with self._locked():
            with self._db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS receipts (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)")
            self._ref()
            self._head()
            self._recover()

    def _git(self, *args: str, data: bytes | None = None, index: Path | None = None) -> bytes:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({
            "GIT_AUTHOR_NAME": "VaultWrite", "GIT_AUTHOR_EMAIL": "vaultwrite@localhost",
            "GIT_COMMITTER_NAME": "VaultWrite", "GIT_COMMITTER_EMAIL": "vaultwrite@localhost",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        try:
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                 "--literal-pathspecs", *args],
                cwd=self.root, env=env, input=data, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VaultError("GIT_ERROR", str(exc)) from exc
        if result.returncode:
            raise VaultError("GIT_ERROR", result.stderr.decode(errors="replace").strip() or "Git command failed.")
        return result.stdout

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

    def _atomic_write(self, path: Path, data: bytes, mode: int = 0o600) -> None:
        self._ensure_parent(path.parent)
        fd, temporary = tempfile.mkstemp(prefix=".vault-write-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

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

    def _checkpoint(self, stage: str) -> None:
        """Overridden by failure-injection tests; no runtime bypass or failpoint."""

    def _recover(self) -> None:
        if not self.journal.exists():
            return
        txn = json.loads(self.journal.read_text(encoding="utf-8"))
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
            message = f"{operation} {path}\n\nVaultWrite-Actor: {actor}\nVaultWrite-Request: {request_id}\n"
            commit = self._git("commit-tree", tree, "-p", head, data=message.encode()).decode().strip()
            return blob, commit
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)

    def write_note(self, path: str, content: str, expected_version: str, request_id: str, actor: str) -> dict:
        return self._mutate("write", path, content, expected_version, request_id, actor)

    def append_note(self, path: str, content: str, expected_version: str, request_id: str, actor: str) -> dict:
        return self._mutate("append", path, content, expected_version, request_id, actor)

    def _mutate(self, operation: str, path: str, content: str, expected_version: str, request_id: str, actor: str) -> dict:
        if not isinstance(expected_version, str) or not re.fullmatch(r"missing|sha256:[0-9a-f]{64}", expected_version):
            raise VaultError("INVALID_VERSION", "expected_version must be 'missing' or a read_note SHA-256 version; bypasses are not supported.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            raise VaultError("INVALID_REQUEST_ID", "request_id must contain 1–128 ASCII letters, digits, or _ . : -.")
        if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,64}", actor):
            raise VaultError("INVALID_ACTOR", "actor must contain 1–64 ASCII letters, digits, or _ . @ : -.")
        if not isinstance(content, str):
            raise VaultError("INVALID_CONTENT", "content must be a UTF-8 string.")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise VaultError("INVALID_ENCODING", "content must be valid UTF-8.") from exc
        if len(encoded) > self.MAX_BYTES:
            raise VaultError("TOO_LARGE", "Notes must be at most 1 MiB.")
        fingerprint = hashlib.sha256(json.dumps([operation, path, content, expected_version, actor], ensure_ascii=True).encode()).hexdigest()
        with self._locked():
            self._recover()
            target = self._path(path)
            previous = self._receipt(request_id, fingerprint)
            if previous is not None:
                return previous
            old = self._read(target)
            if version(old) != expected_version:
                raise VaultError("VERSION_CONFLICT", "Re-read the note, merge your change, and retry.", current_version=version(old))
            if operation == "append" and old is None:
                raise VaultError("NOT_FOUND", "Create the note with write_note before appending.")
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
            if entry:
                metadata = entry.split(b"\t", 1)[0].decode().split()
                git_mode, kind, old_blob = metadata
                if kind != "blob" or git_mode not in ("100644", "100755"):
                    raise VaultError("INVALID_PATH", "Only regular Git files are supported.")
                committed = self._git("cat-file", "blob", old_blob)
                if committed != old:
                    raise VaultError("DIRTY_TARGET", "The note differs from HEAD; commit external edits before writing.")
            elif old is not None:
                raise VaultError("UNTRACKED_TARGET", "Commit this existing untracked note before writing.")
            result = {"path": path, "version": version(new), "commit": head, "request_id": request_id, "changed": new != old, "replayed": False}
            if new == old:
                self._save_receipt(request_id, fingerprint, result)
                return result
            attributes = self._git("check-attr", "-z", "filter", "working-tree-encoding", "--", path).split(b"\0")
            if attributes[2::3] != [b"unspecified", b"unspecified"]:
                raise VaultError("UNSUPPORTED_ATTRIBUTES", "Git filters and working-tree-encoding are not supported for managed notes.")
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

    def note_history(self, path: str, limit: int = 20) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise VaultError("INVALID_LIMIT", "limit must be between 1 and 100.")
        with self._locked():
            self._recover()
            self._path(path)
            output = self._git("log", f"-{limit}", "--format=%H%x00%at%x00%s", "--", path).decode()
            commits = []
            for line in output.splitlines():
                commit, timestamp, subject = line.split("\0", 2)
                commits.append({"commit": commit, "timestamp": int(timestamp), "subject": subject})
            return {"path": path, "commits": commits}


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
