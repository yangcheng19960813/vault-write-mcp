"""Read-only diagnostic and rename-history demo using only a fresh temp vault."""

import json
from pathlib import Path
import tempfile

from vault_write_mcp.diagnostics import doctor
from vault_write_mcp.store import VaultStore, init_vault


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def main():
    with tempfile.TemporaryDirectory(prefix="vault-doctor-demo-") as temporary:
        root = Path(temporary)
        init_vault(root)
        store = VaultStore(root)
        store.write_note("before.md", "# Synthetic example\n", "missing", "seed", "demo")
        plan = store.preview_move_note("before.md", "after.md")
        store.move_note("before.md", "after.md", plan["expected_versions"], plan["plan_version"], "move", "demo")
        # doctor intentionally does not inspect worktree/index cleanliness:
        # even with an external edit, tracked_changes is unknown, not changed.
        with (root / "after.md").open("ab") as stream:
            stream.write(b"Uncommitted synthetic external edit\n")
        before = snapshot(root)
        result = doctor(root)
        assert snapshot(root) == before, "Diagnostic changed vault files"
        assert result["vault"]["tracked_changes"] == "unknown"
        print(json.dumps({"doctor": result, "history": store.note_history("after.md", follow_renames=True)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
