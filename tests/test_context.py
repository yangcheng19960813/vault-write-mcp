"""Bounded contextual reads and snapshot-stable patch receipt semantics."""

import hashlib
import json
import multiprocessing
import os

import pytest

from vault_write_mcp.config import CommitConfig, Settings
from vault_write_mcp.editing import MAX_CONTEXT_LINES, MAX_READ_BYTES, anchor_for, rendered_cost
from vault_write_mcp.store import VaultError, VaultStore, init_vault


@pytest.fixture
def store(tmp_path):
    init_vault(tmp_path)
    return VaultStore(tmp_path)


def seed(store, content):
    store.write_note("note.md", content, "missing", "seed", "test")
    return store.read_note_range("note.md")


def edit(snapshot, number=1, lines=None):
    original = next(line for line in snapshot["lines"] if line.startswith(f"{number}:"))
    return {"op": "SWAP", "anchor": original.split("|", 1)[0], "lines": lines or ["NEW"]}


def patch(store, snapshot, edits=None, request_id="patch", **options):
    return store.patch_note("note.md", edits or [edit(snapshot)], snapshot["version"], request_id, "test", **options)


def window_lines(windows):
    return [line for window in windows for line in window["lines"]]


def numbers(entries):
    return [int(line.split(":", 1)[0]) for line in entries]


def assert_windows(windows, expected_numbers=None):
    entries = window_lines(windows)
    assert len(entries) <= MAX_CONTEXT_LINES
    assert numbers(entries) == sorted(set(numbers(entries)))
    for previous, current in zip(windows, windows[1:]):
        assert previous["end_line"] + 1 < current["start_line"]
    for window in windows:
        assert numbers(window["lines"]) == list(range(window["start_line"], window["end_line"] + 1))
    if expected_numbers is not None:
        assert numbers(entries) == expected_numbers


def test_find_defaults_are_exactly_backward_compatible_and_context_merges_adjacency(store):
    seed(store, "one\ntwo\nHIT\nfour\nfive\nHIT\nseven\neight\n")
    old = store.find_note("note.md", "HIT")
    assert set(old) == {"path", "exists", "version", "total_lines", "matches", "next_line"}
    assert old == store.find_note("note.md", "HIT", context_before=0, context_after=0)
    contextual = store.find_note("note.md", "HIT", context_before=1, context_after=1)
    assert contextual["matches"] == old["matches"] and contextual["version"] == old["version"]
    assert len(contextual["windows"]) == 1 and not contextual["context_truncated"]
    assert_windows(contextual["windows"], list(range(2, 8)))


def test_context_that_contains_a_future_match_never_consumes_it(store):
    seed(store, "HIT\nHIT\nHIT\nHIT\n")
    seen, cursor = [], 1
    while cursor is not None:
        page = store.find_note("note.md", "HIT", start_line=cursor, max_matches=1, context_before=50, context_after=50)
        assert_windows(page["windows"], [1, 2, 3, 4])
        seen.extend(numbers(page["matches"]))
        cursor = page["next_line"]
    assert seen == [1, 2, 3, 4]


def test_find_pages_obey_combined_byte_line_budgets_without_lost_matches(store):
    content = "".join(("HIT " if number % 3 == 0 else "other ") + "中文" * (40 + number % 80) + "\n" for number in range(900))
    seed(store, content)
    seen, cursor, versions = [], 1, set()
    while cursor is not None:
        page = store.find_note("note.md", "HIT", cursor, 50, 50, 50)
        assert_windows(page["windows"])
        assert sum(rendered_cost(line) for line in page["matches"] + window_lines(page["windows"])) <= MAX_READ_BYTES
        assert page["context_truncated"]
        seen.extend(numbers(page["matches"]))
        versions.add(page["version"])
        next_cursor = page["next_line"]
        assert next_cursor is None or next_cursor > cursor
        cursor = next_cursor
    assert seen == list(range(1, 901, 3)) and len(versions) == 1


def test_find_oversized_neighbor_does_not_hide_the_match(store):
    store.write_note("note.md", "x" * (MAX_READ_BYTES + 1) + "\nHIT\ntail\n", "missing", "seed", "test")
    result = store.find_note("note.md", "HIT", context_before=1, context_after=1)
    assert numbers(result["matches"]) == [2]
    assert_windows(result["windows"], [2, 3])
    assert result["context_truncated"]


@pytest.mark.parametrize("content", [None, "", "\ufeff"])
def test_find_context_on_missing_and_empty_notes(store, content):
    if content is not None:
        seed(store, content)
    found = store.find_note("note.md", "missing", context_before=50, context_after=50)
    assert found["windows"] == [] and not found["context_truncated"]
    assert found["matches"] == [] and found["next_line"] is None


@pytest.mark.parametrize("name", ["context_before", "context_after"])
@pytest.mark.parametrize("bad", [True, False, -1, 51, 1.5, "1", None])
def test_context_distances_are_strict_and_bounded(store, name, bad):
    with pytest.raises(VaultError) as caught:
        store.find_note("note.md", "x", **{name: bad})
    assert caught.value.code == "INVALID_RANGE"
    snapshot = seed(store, "one\n")
    with pytest.raises(VaultError) as caught:
        patch(store, snapshot, return_context=True, **{name: bad})
    assert caught.value.code == "INVALID_RANGE"
    assert store.read_note("note.md")["version"] == snapshot["version"]


def test_patch_context_returns_shifted_post_edit_anchors_and_merged_windows(store):
    snapshot = seed(store, "\ufeffone\r\ntwo\r\nthree\r\nfour\r\nfive")
    result = patch(store, snapshot, [edit(snapshot, 1, ["A", "B"]), edit(snapshot, 4, ["D"])], return_context=True, context_after=1)
    assert result["result_context"]["total_lines"] == 6
    assert not result["result_context"]["truncated"]
    windows = result["result_context"]["windows"]
    assert_windows(windows, [1, 2, 3, 5, 6])
    current = store.read_note_range("note.md")
    assert window_lines(windows) == [line for line in current["lines"] if int(line.split(":", 1)[0]) in (1, 2, 3, 5, 6)]
    # Fresh returned anchors support the next patch without another read.
    followup = {"version": result["version"], "lines": window_lines(windows)}
    patch(store, followup, [edit(followup, 5, ["next"])], "next")
    assert store.read_note("note.md")["content"] == "\ufeffA\r\nB\r\ntwo\r\nthree\r\nnext\r\nfive"


def test_patch_default_response_unchanged_and_explicit_return_context_false_is_same_request(store):
    snapshot = seed(store, "one\ntwo\n")
    result = patch(store, snapshot)
    assert set(result) == {"path", "version", "commit", "request_id", "changed", "replayed"}
    replay = patch(store, snapshot, return_context=False, context_before=0, context_after=0)
    assert replay == {**result, "replayed": True}
    with pytest.raises(VaultError) as caught:
        patch(store, snapshot, return_context=True)
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("changes,expected", [
    ({"op": "INS.PRE", "number": 2, "lines": ["inserted"]}, [2, 3]),
    ({"op": "INS.POST", "number": 2, "lines": ["inserted"]}, [2, 3]),
    ({"op": "DEL", "number": 1}, [1]),
    ({"op": "DEL", "number": 3}, [2]),
    ({"op": "SWAP", "number": 2, "lines": ["TWO", "second"]}, [2, 3]),
])
def test_patch_zero_surrounding_context_still_returns_edit_and_deletion_boundary(store, changes, expected):
    snapshot = seed(store, "one\ntwo\nthree")
    operation = {"op": changes["op"], "anchor": edit(snapshot, changes["number"])["anchor"]}
    if "lines" in changes:
        operation["lines"] = changes["lines"]
    result = patch(store, snapshot, [operation], return_context=True)
    assert_windows(result["result_context"]["windows"], expected)
    current = store.read_note_range("note.md")
    assert window_lines(result["result_context"]["windows"]) == [line for line in current["lines"] if int(line.split(":", 1)[0]) in expected]


def test_delete_everything_and_noop_context_receipts(store):
    snapshot = seed(store, "one\n")
    noop = patch(store, snapshot, [edit(snapshot, 1, ["one"])], "noop", return_context=True)
    assert not noop["changed"] and not noop["result_context"]["truncated"]
    assert_windows(noop["result_context"]["windows"], [1])
    deleted = patch(store, snapshot, [{"op": "DEL", "anchor": edit(snapshot)["anchor"]}], "delete", return_context=True)
    assert deleted["result_context"] == {"total_lines": 0, "windows": [], "truncated": False}
    assert patch(store, snapshot, [edit(snapshot, 1, ["one"])], "noop", return_context=True)["result_context"] == noop["result_context"]


@pytest.mark.parametrize("options", [{"return_context": 1}, {"return_context": "true"}, {"context_before": 1}, {"context_after": 1}])
def test_patch_rejects_ambiguous_result_options_without_mutation(store, options):
    snapshot = seed(store, "one\n")
    with pytest.raises(VaultError) as caught:
        patch(store, snapshot, **options)
    assert caught.value.code == "INVALID_CONTEXT"
    assert store.read_note("note.md")["version"] == snapshot["version"]


@pytest.mark.parametrize("mode", ["immediate", "interval"])
def test_patch_replays_original_context_after_later_edits_restart_and_batch_flush(store, mode):
    snapshot = seed(store, "one\ntwo\nthree\n")
    configured = VaultStore(store.root, Settings(commit=CommitConfig(mode=mode)))
    options = {"return_context": True, "context_before": 1, "context_after": 1}
    first = patch(configured, snapshot, [edit(snapshot, 2, ["TWO", "new"])], **options)
    current = configured.read_note_range("note.md")
    patch(configured, current, [edit(current, 2, ["FUTURE"])], "later")
    restarted = VaultStore(store.root, configured.settings)
    restarted.flush()
    replay = patch(restarted, snapshot, [edit(snapshot, 2, ["TWO", "new"])], **options)
    assert replay["replayed"] and replay["result_context"] == first["result_context"] and replay["version"] == first["version"]
    assert not any("FUTURE" in line for line in window_lines(replay["result_context"]["windows"]))
    for changed in ({"return_context": False}, {**options, "context_before": 2}, {**options, "context_after": 2}):
        with pytest.raises(VaultError) as caught:
            patch(restarted, snapshot, [edit(snapshot, 2, ["TWO", "new"])], **changed)
        assert caught.value.code == "IDEMPOTENCY_CONFLICT"


def test_preupgrade_receipts_remain_replayable_without_synthesizing_future_context(store):
    snapshot = seed(store, "one\n")
    edits = [edit(snapshot)]
    arguments = ["patch", "note.md", edits, snapshot["version"], "test"]
    fingerprint = hashlib.sha256(json.dumps(arguments, ensure_ascii=True).encode()).hexdigest()
    legacy = {"path": "note.md", "version": "sha256:" + "0" * 64, "commit": "old-commit", "request_id": "legacy", "changed": True, "replayed": False}
    with store._db() as db:
        db.execute("INSERT INTO receipts VALUES (?, ?, ?)", ("legacy", fingerprint, json.dumps(legacy)))
    restarted = VaultStore(store.root)
    assert patch(restarted, snapshot, edits, "legacy") == {**legacy, "replayed": True}
    with pytest.raises(VaultError) as caught:
        patch(restarted, snapshot, edits, "legacy", return_context=True)
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("replacement", [["x" * (MAX_READ_BYTES + 1)], ["new"] * 500])
def test_patch_context_budget_never_rejects_a_valid_large_edit(store, replacement):
    snapshot = seed(store, "one\n")
    result = patch(store, snapshot, [edit(snapshot, lines=replacement)], return_context=True, context_before=50, context_after=50)
    context = result["result_context"]
    assert context["truncated"]
    assert_windows(context["windows"])
    assert sum(rendered_cost(line) for line in window_lines(context["windows"])) <= MAX_READ_BYTES
    assert store.read_note("note.md")["version"] == result["version"]


def crash_context_patch(root, mode, stage):
    class CrashingStore(VaultStore):
        def _checkpoint(self, actual):
            if actual == stage:
                os._exit(77)
    store = CrashingStore(root, Settings(commit=CommitConfig(mode=mode)))
    current = store.read_note_range("note.md")
    patch(store, current, [edit(current, 1, ["NEW", "inserted"])], return_context=True, context_after=1)


@pytest.mark.parametrize("mode,stage", [
    ("immediate", "prepared"), ("immediate", "file_written"), ("immediate", "ref_updated"), ("immediate", "receipt_saved"),
    ("interval", "prepared"), ("interval", "file_written"), ("interval", "receipt_saved"),
])
def test_context_receipt_and_write_recover_atomically_after_process_exit(store, mode, stage):
    snapshot = seed(store, "one\ntwo\nthree\n")
    child = multiprocessing.get_context("spawn").Process(target=crash_context_patch, args=(str(store.root), mode, stage))
    child.start()
    child.join(timeout=30)
    assert child.exitcode == 77
    restarted = VaultStore(store.root, Settings(commit=CommitConfig(mode=mode)))
    later = restarted.read_note_range("note.md")
    patch(restarted, later, [edit(later, 1, ["FUTURE"])], "later")
    restarted.flush()
    replay = patch(restarted, snapshot, [edit(snapshot, 1, ["NEW", "inserted"])], return_context=True, context_after=1)
    assert replay["replayed"]
    assert replay["result_context"] == {"total_lines": 4, "windows": [{"start_line": 1, "end_line": 3, "lines": [
        f'{anchor_for(1, "NEW" + chr(10))}|NEW', f'{anchor_for(2, "inserted" + chr(10))}|inserted', f'{anchor_for(3, "two" + chr(10))}|two',
    ]}], "truncated": False}
    assert not restarted.journal.exists()
