from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
import subprocess

import pytest

from vault_write_mcp.config import CommitConfig, Settings
from vault_write_mcp.editing import MAX_READ_BYTES
from vault_write_mcp.store import VaultError, VaultStore, init_vault


@pytest.fixture
def store(tmp_path):
    init_vault(tmp_path)
    return VaultStore(tmp_path)


def seed(store, content="one\ntwo\nthree\nfour\nfive\n"):
    store.write_note("note.md", content, "missing", "seed", "test")
    return store.read_note_range("note.md")


def anchor(snapshot, number):
    return next(line.split("|", 1)[0] for line in snapshot["lines"] if line.startswith(f"{number}:"))


def patch(store, snapshot, edits, request_id="patch"):
    return store.patch_note("note.md", edits, snapshot["version"], request_id, "test")


def expect_error(code, method, *args, **kwargs):
    with pytest.raises(VaultError) as caught:
        method(*args, **kwargs)
    assert caught.value.code == code


def test_bounded_range_has_whole_note_version_and_continuation(store):
    content = "".join(f"line {number} " + "中文" * 30 + "\n" for number in range(5000))
    seed(store, content)
    result = store.read_note_range("note.md", 2100, 200)
    assert result["version"] == store.read_note("note.md")["version"]
    assert result["total_lines"] == 5000 and result["start_line"] == 2100
    assert result["lines"][0].endswith("|line 2099 " + "中文" * 30)
    assert len(json.dumps(result["lines"], ensure_ascii=False).encode()) <= MAX_READ_BYTES
    assert 2100 < result["next_line"] < 2300
    continued = store.read_note_range("note.md", result["next_line"], 1)
    assert continued["lines"][0].startswith(f'{result["next_line"]}:')
    assert "content" not in result


def test_find_is_literal_case_sensitive_and_pageable(store):
    seed(store, "Target [a.*]\nother\nTarget [a.*]\ntarget [ax]\nTarget [a.*]\n")
    first = store.find_note("note.md", "[a.*]", max_matches=1)
    assert len(first["matches"]) == 1 and first["matches"][0].startswith("1:")
    assert first["next_line"] == 2
    second = store.find_note("note.md", "[a.*]", start_line=2, max_matches=2)
    assert [line.split(":")[0] for line in second["matches"]] == ["3", "5"]
    assert second["next_line"] is None
    assert store.find_note("note.md", "TARGET")["matches"] == []
    assert first["version"] == second["version"]


def test_missing_empty_and_final_newline_have_unambiguous_line_counts(store):
    missing = store.read_note_range("note.md")
    assert missing["version"] == "missing" and not missing["exists"] and missing["lines"] == []
    assert store.find_note("note.md", "x")["matches"] == []
    empty = seed(store, "")
    assert empty["exists"] and empty["total_lines"] == 0
    store.write_note("note.md", "a\n\n", empty["version"], "blank", "test")
    assert store.read_note_range("note.md")["total_lines"] == 2
    assert store.read_note_range("note.md", 99)["lines"] == []
    expect_error("NOT_FOUND", store.patch_note, "missing.md", [{"op": "DEL", "anchor": "1:000000000000"}], "missing", "absent", "test")


def test_large_single_line_is_not_silently_truncated(store):
    store.write_note("note.md", "x" * (MAX_READ_BYTES + 1), "missing", "seed", "test")
    expect_error("LINE_TOO_LONG", store.read_note_range, "note.md")
    expect_error("LINE_TOO_LONG", store.find_note, "note.md", "x")


@pytest.mark.parametrize("method,options", [
    ("read_note_range", {"start_line": 0}),
    ("read_note_range", {"start_line": True}),
    ("read_note_range", {"max_lines": 201}),
    ("find_note", {"text": ""}),
    ("find_note", {"text": "\n"}),
    ("find_note", {"text": "a", "max_matches": 51}),
])
def test_read_limits_are_validated(store, method, options):
    code = "INVALID_QUERY" if options.get("text") in ("", "\n") else "INVALID_RANGE"
    expect_error(code, getattr(store, method), "note.md", **options)


def test_all_edits_use_original_line_numbers_and_one_git_commit(store):
    snapshot = seed(store)
    result = patch(store, snapshot, [
        {"op": "INS.POST", "anchor": anchor(snapshot, 5), "lines": ["six", "seven"]},
        {"op": "DEL", "anchor": anchor(snapshot, 3), "end_anchor": anchor(snapshot, 4)},
        {"op": "SWAP", "anchor": anchor(snapshot, 2), "lines": ["TWO", "second"]},
        {"op": "INS.PRE", "anchor": anchor(snapshot, 1), "lines": ["zero"]},
    ])
    expected = "zero\none\nTWO\nsecond\nfive\nsix\nseven\n"
    assert store.read_note("note.md")["content"] == expected
    assert "content" not in result and "lines" not in result
    assert len(store.note_history("note.md")["commits"]) == 2
    assert subprocess.check_output(["git", "-C", str(store.root), "show", result["commit"] + ":note.md"]).decode() == expected
    assert subprocess.check_output(["git", "-C", str(store.root), "status", "--porcelain"]) == b""


@pytest.mark.parametrize("bom", ["", "\ufeff"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("final_newline", [False, True])
def test_bom_crlf_and_eof_survive_replace_and_append_lines(store, bom, newline, final_newline):
    snapshot = seed(store, bom + "头" + newline + "尾" + (newline if final_newline else ""))
    assert snapshot["lines"][0].endswith("|头")
    patch(store, snapshot, [
        {"op": "SWAP", "anchor": anchor(snapshot, 1), "lines": ["新头", "更多"]},
        {"op": "INS.POST", "anchor": anchor(snapshot, 2), "lines": ["新尾", ""]},
    ])
    expected = bom + newline.join(["新头", "更多", "尾", "新尾", ""]) + (newline if final_newline else "")
    assert (store.root / "note.md").read_bytes() == expected.encode()


def test_block_replacement_and_deletion_preserve_unedited_bytes(store):
    snapshot = seed(store, "\ufeffa\r\nb\nc\r\nd\nlast")
    patch(store, snapshot, [{"op": "SWAP", "anchor": anchor(snapshot, 2), "end_anchor": anchor(snapshot, 4), "lines": ["B", "C"]}])
    assert (store.root / "note.md").read_bytes() == "\ufeffa\r\nB\nC\nlast".encode()
    current = store.read_note_range("note.md")
    patch(store, current, [{"op": "DEL", "anchor": anchor(current, 1), "end_anchor": anchor(current, 4)}], "delete-all")
    assert (store.root / "note.md").read_bytes() == b"\xef\xbb\xbf"


def test_unicode_separators_and_duplicate_lines_are_not_ambiguous(store):
    snapshot = seed(store, "same\nsame\ntext\u2028inside\n")
    assert snapshot["total_lines"] == 3 and anchor(snapshot, 1) != anchor(snapshot, 2)
    patch(store, snapshot, [{"op": "SWAP", "anchor": anchor(snapshot, 2), "lines": ["changed"]}])
    assert store.read_note("note.md")["content"] == "same\nchanged\ntext\u2028inside\n"


def test_stale_version_and_later_bad_anchor_reject_entire_patch(store):
    snapshot = seed(store)
    edits = [{"op": "SWAP", "anchor": anchor(snapshot, 1), "lines": ["ONE"]}]
    store.append_note("note.md", "six\n", snapshot["version"], "append", "other")
    expect_error("VERSION_CONFLICT", patch, store, snapshot, edits)
    current = store.read_note_range("note.md")
    before = store.read_note("note.md")
    edits.append({"op": "DEL", "anchor": "2:000000000000"})
    expect_error("ANCHOR_CONFLICT", patch, store, current, edits)
    assert store.read_note("note.md") == before
    assert len(store.note_history("note.md")["commits"]) == 2
    assert not store.journal.exists()


@pytest.mark.parametrize("edits,code", [
    ([], "INVALID_PATCH"),
    ([{"op": "unknown", "anchor": "1:000000000000"}], "INVALID_PATCH"),
    ([{"op": "SWAP", "anchor": "1:000000000000", "line": ["typo"]}], "INVALID_PATCH"),
    ([{"op": "SWAP", "anchor": "1:000000000000", "lines": ["a\nb"]}], "INVALID_PATCH"),
    ([{"op": "SWAP", "anchor": "1:000000000000", "lines": []}], "INVALID_PATCH"),
    ([{"op": "DEL", "anchor": "1:000000000000", "lines": ["ignored?"]}], "INVALID_PATCH"),
    ([{"op": "INS.PRE", "anchor": "1:000000000000", "end_anchor": "1:000000000000", "lines": ["a"]}], "INVALID_PATCH"),
    ([{"op": "DEL", "anchor": "1:xx"}], "INVALID_ANCHOR"),
    ([{"op": "SWAP", "anchor": "1:000000000000", "lines": ["\ud800"]}], "INVALID_ENCODING"),
])
def test_invalid_edits_are_rejected_without_writes(store, edits, code):
    snapshot = seed(store)
    before = store.read_note("note.md")
    expect_error(code, patch, store, snapshot, edits)
    assert store.read_note("note.md") == before


def test_overlapping_reversed_and_reused_anchors_are_rejected(store):
    snapshot = seed(store)
    a, b, c = [anchor(snapshot, number) for number in (1, 2, 3)]
    expect_error("INVALID_PATCH", patch, store, snapshot, [{"op": "DEL", "anchor": c, "end_anchor": a}])
    expect_error("OVERLAPPING_EDITS", patch, store, snapshot, [
        {"op": "DEL", "anchor": a, "end_anchor": c},
        {"op": "INS.PRE", "anchor": b, "lines": ["middle"]},
    ])
    expect_error("OVERLAPPING_EDITS", patch, store, snapshot, [
        {"op": "INS.PRE", "anchor": a, "lines": ["before"]},
        {"op": "INS.POST", "anchor": a, "lines": ["after"]},
    ])
    assert store.read_note("note.md")["version"] == snapshot["version"]


@pytest.mark.parametrize("mode", ["immediate", "interval"])
def test_patch_replay_survives_restart_and_flush(store, mode):
    snapshot = seed(store)
    configured = VaultStore(store.root, Settings(commit=CommitConfig(mode=mode)))
    edits = [{"op": "INS.POST", "anchor": anchor(snapshot, 1), "lines": ["added"]}]
    result = patch(configured, snapshot, edits)
    restarted = VaultStore(store.root, configured.settings)
    assert patch(restarted, snapshot, edits)["replayed"]
    if mode == "interval":
        assert result["pending"] and result["commit"] is None
        commit = restarted.flush()["commit"]
        replay = patch(restarted, snapshot, edits)
        assert not replay["pending"] and replay["commit"] == commit
    changed = [{**edits[0], "lines": ["different"]}]
    expect_error("IDEMPOTENCY_CONFLICT", patch, restarted, snapshot, changed)
    assert restarted.read_note("note.md")["content"].count("added\n") == 1
    assert len(restarted.note_history("note.md")["commits"]) == 2


def test_noop_patch_does_not_commit_and_keeps_shared_request_namespace(store):
    snapshot = seed(store)
    result = patch(store, snapshot, [{"op": "SWAP", "anchor": anchor(snapshot, 1), "lines": ["one"]}])
    assert not result["changed"] and len(store.note_history("note.md")["commits"]) == 1
    expect_error("IDEMPOTENCY_CONFLICT", store.write_note, "note.md", "different", snapshot["version"], "patch", "test")


def test_patch_preserves_external_changes_and_rejects_staging(store):
    snapshot = seed(store)
    edits = [{"op": "DEL", "anchor": anchor(snapshot, 1)}]
    (store.root / "other.md").write_text("unrelated")
    subprocess.run(["git", "-C", str(store.root), "add", "other.md"], check=True)
    expect_error("STAGED_CHANGES", patch, store, snapshot, edits)
    subprocess.run(["git", "-C", str(store.root), "restore", "--staged", "other.md"], check=True)
    (store.root / "note.md").write_text("external\n")
    current = store.read_note_range("note.md")
    expect_error("DIRTY_TARGET", patch, store, current, [{"op": "DEL", "anchor": anchor(current, 1)}])
    assert (store.root / "note.md").read_text() == "external\n"
    assert (store.root / "other.md").read_text() == "unrelated"


def concurrent_patch(root, barrier, queue, number):
    store = VaultStore(root)
    snapshot = store.read_note_range("note.md")
    barrier.wait(timeout=20)
    try:
        patch(store, snapshot, [{"op": "SWAP", "anchor": anchor(snapshot, number), "lines": [f"agent-{number}"]}], f"agent-{number}")
        queue.put((number, "success"))
    except VaultError as exc:
        queue.put((number, exc.code))


def test_cross_process_patch_conflict_then_bounded_retry_keeps_both_changes(store):
    seed(store)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=concurrent_patch, args=(str(store.root), barrier, queue, number)) for number in (1, 2)]
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert sorted(status for _, status in results) == ["VERSION_CONFLICT", "success"]
    loser = next(number for number, status in results if status == "VERSION_CONFLICT")
    current = store.read_note_range("note.md", loser, 1)
    patch(store, current, [{"op": "SWAP", "anchor": anchor(current, loser), "lines": [f"agent-{loser}"]}], "retry")
    assert store.read_note("note.md")["content"].startswith("agent-1\nagent-2\n")


def crash_patch(root, mode, stage):
    class CrashingStore(VaultStore):
        def _checkpoint(self, actual):
            if actual == stage:
                os._exit(77)

    store = CrashingStore(root, Settings(commit=CommitConfig(mode=mode)))
    snapshot = store.read_note_range("note.md")
    patch(store, snapshot, [{"op": "INS.POST", "anchor": anchor(snapshot, 1), "lines": ["durable"]}])


@pytest.mark.parametrize("mode,stage", [
    ("immediate", "prepared"), ("immediate", "file_written"), ("immediate", "ref_updated"), ("immediate", "receipt_saved"),
    ("interval", "prepared"), ("interval", "file_written"), ("interval", "receipt_saved"),
])
def test_patch_recovers_after_actual_process_exit(store, mode, stage):
    snapshot = seed(store)
    worker = multiprocessing.get_context("spawn").Process(target=crash_patch, args=(str(store.root), mode, stage))
    worker.start()
    worker.join(timeout=20)
    assert worker.exitcode == 77
    restarted = VaultStore(store.root, Settings(commit=CommitConfig(mode=mode)))
    restarted.flush()
    replay = patch(restarted, snapshot, [{"op": "INS.POST", "anchor": anchor(snapshot, 1), "lines": ["durable"]}])
    assert replay["replayed"]
    assert restarted.read_note("note.md")["content"].count("durable\n") == 1
    assert len(restarted.note_history("note.md")["commits"]) == 2
    assert not restarted.journal.exists()


def test_identical_concurrent_patch_requests_only_apply_once(store):
    snapshot = seed(store)
    edits = [{"op": "INS.PRE", "anchor": anchor(snapshot, 1), "lines": ["once"]}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: patch(VaultStore(store.root), snapshot, edits), range(2)))
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert store.read_note("note.md")["content"].count("once\n") == 1
