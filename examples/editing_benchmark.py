"""Compare bounded edit workflows on an isolated temporary VaultWrite vault.

The metric counts calls to VaultStore methods and UTF-8 JSON bytes in their
result dictionaries. It intentionally excludes setup, final verification, and
HTTP/MCP framing; it does not estimate model tokens or cost.
"""

import json
import re
import tempfile
import uuid

from vault_write_mcp.store import VaultStore, init_vault


ANCHOR_LINE = re.compile(r"^([1-9][0-9]*:[0-9a-f]{12})\|(.*)$")


class Tally:
    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.returned_bytes = 0
        self.steps = []

    def call(self, name, *args, **kwargs):
        result = getattr(self.store, name)(*args, **kwargs)
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.calls += 1
        self.returned_bytes += size
        self.steps.append({"tool": name, "returned_utf8_bytes": size})
        return result


def rendered_lines(value):
    """Yield anchor/text pairs wherever rendered line strings occur in a result."""
    if isinstance(value, str):
        match = ANCHOR_LINE.match(value)
        if match:
            yield match.group(1), match.group(2)
    elif isinstance(value, dict):
        for item in value.values():
            yield from rendered_lines(item)
    elif isinstance(value, list):
        for item in value:
            yield from rendered_lines(item)


def anchor_for_text(result, phrase):
    for anchor, line in rendered_lines(result):
        if phrase in line:
            return anchor
    raise RuntimeError(f"No returned anchor contains {phrase!r}; got: {result!r}")


def range_around(tally, path, anchor, radius=3):
    number = int(anchor.split(":", 1)[0])
    return tally.call("read_note_range", path, max(1, number - radius), radius * 2 + 1)


def initial_content():
    lines = [f"background {number:04d}: 項目備忘與狀態資料。" for number in range(100)]
    lines[40] = "TARGET_A: revise this sentence"
    lines[44] = "TARGET_B: revise the related sentence"
    return lines


def perform_baseline(store, path):
    tally = Tally(store)

    found_a = tally.call("find_note", path, "TARGET_A")
    range_a = range_around(tally, path, anchor_for_text(found_a, "TARGET_A"))
    anchor_a = anchor_for_text(range_a, "TARGET_A")
    patched_a = tally.call(
        "patch_note", path,
        [{"op": "SWAP", "anchor": anchor_a, "lines": [
            "TARGET_A: revised wording", "TARGET_A: updated detail",
        ]}],
        range_a["version"], f"baseline-a-{uuid.uuid4().hex[:12]}", "benchmark",
    )

    # The first patch inserted a line, so the old line-numbered anchor for B is
    # stale. The defaults-only workflow searches and reads its current context.
    found_b = tally.call("find_note", path, "TARGET_B")
    range_b = range_around(tally, path, anchor_for_text(found_b, "TARGET_B"))
    anchor_b = anchor_for_text(range_b, "TARGET_B")
    tally.call(
        "patch_note", path,
        [{"op": "SWAP", "anchor": anchor_b, "lines": ["TARGET_B: revised wording"]}],
        range_b["version"], f"baseline-b-{uuid.uuid4().hex[:12]}", "benchmark",
    )
    return tally


def perform_context_workflow(store, path):
    tally = Tally(store)

    found_a = tally.call(
        "find_note", path, "TARGET_A", context_before=1, context_after=6,
    )
    anchor_a = anchor_for_text(found_a, "TARGET_A")
    patched_a = tally.call(
        "patch_note", path,
        [{"op": "SWAP", "anchor": anchor_a, "lines": [
            "TARGET_A: revised wording", "TARGET_A: updated detail",
        ]}],
        found_a["version"], f"context-a-{uuid.uuid4().hex[:12]}", "benchmark",
        return_context=True, context_before=1, context_after=6,
    )

    # Use the post-edit anchor and version, including the shifted line number,
    # to make the second edit without another find or range call.
    context = patched_a["result_context"]
    anchor_b = anchor_for_text(context, "TARGET_B")
    tally.call(
        "patch_note", path,
        [{"op": "SWAP", "anchor": anchor_b, "lines": ["TARGET_B: revised wording"]}],
        patched_a["version"], f"context-b-{uuid.uuid4().hex[:12]}", "benchmark",
    )
    return tally


def expected_content():
    lines = initial_content()
    lines[40:41] = ["TARGET_A: revised wording", "TARGET_A: updated detail"]
    # B was originally at index 44 and shifted to index 45 after the insertion.
    lines[45] = "TARGET_B: revised wording"
    return "\n".join(lines) + "\n"


def main():
    run_id = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="vault-write-edit-benchmark-") as directory:
        root = init_vault(directory)
        store = VaultStore(root)
        content = "\n".join(initial_content()) + "\n"
        base_path = f"benchmark/{run_id}-base.md"
        context_path = f"benchmark/{run_id}-ctx_.md"
        store.write_note(base_path, content, "missing", f"seed-base-{run_id}", "benchmark")
        store.write_note(context_path, content, "missing", f"seed-ctx-{run_id}", "benchmark")

        baseline = perform_baseline(store, base_path)
        context = perform_context_workflow(store, context_path)
        expected = expected_content()
        actual_base = store.read_note(base_path)["content"]
        actual_context = store.read_note(context_path)["content"]
        if actual_base != expected or actual_context != expected:
            raise AssertionError("The two workflows did not produce the same final note content.")

        saved_bytes = baseline.returned_bytes - context.returned_bytes
        byte_reduction = (100 * saved_bytes / baseline.returned_bytes) if baseline.returned_bytes else 0.0
        report = {
            "task": "Two sequential edits; the first expands one line to two and shifts the second target.",
            "measurement": "Direct VaultStore result dictionaries serialized as compact UTF-8 JSON; excludes setup, verification, and HTTP/MCP framing.",
            "same_final_content": True,
            "defaults_find_range_patch": {
                "calls": baseline.calls,
                "returned_utf8_bytes": baseline.returned_bytes,
                "steps": baseline.steps,
            },
            "context_find_patch_reuse_post_edit_anchor": {
                "calls": context.calls,
                "returned_utf8_bytes": context.returned_bytes,
                "steps": context.steps,
            },
            "difference": {
                "calls_saved": baseline.calls - context.calls,
                "returned_utf8_bytes_saved": saved_bytes,
                "returned_bytes_reduction_percent": round(byte_reduction, 1),
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
