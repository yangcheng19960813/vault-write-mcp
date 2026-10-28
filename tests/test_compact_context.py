"""Opt-in compact find results: deduplicated text windows plus match refs."""

import json

import pytest

from vault_write_mcp.editing import MAX_CONTEXT_LINES, MAX_READ_BYTES, rendered_cost
from vault_write_mcp.store import VaultError, VaultStore, init_vault


@pytest.fixture
def store(tmp_path):
    init_vault(tmp_path)
    return VaultStore(tmp_path)


def seed(store, content):
    store.write_note("note.md", content, "missing", "seed", "test")


def window_lines(result):
    return [line for window in result["windows"] for line in window["lines"]]


def line_number(entry):
    return int(entry.split(":", 1)[0])


def dereference(result, reference):
    window = result["windows"][reference["window_index"]]
    assert line_number(window["lines"][reference["line_index"]]) == reference["line"]
    assert window["start_line"] + reference["line_index"] == reference["line"]
    return window["lines"][reference["line_index"]]


def assert_compact_invariants(result):
    lines = window_lines(result)
    numbers = [line_number(entry) for entry in lines]
    assert numbers == sorted(set(numbers))
    assert len(lines) <= MAX_CONTEXT_LINES
    assert sum(rendered_cost(entry) for entry in lines) <= MAX_READ_BYTES
    for previous, current in zip(result["windows"], result["windows"][1:]):
        assert previous["end_line"] + 1 < current["start_line"]
    for window in result["windows"]:
        assert [line_number(entry) for entry in window["lines"]] == list(
            range(window["start_line"], window["end_line"] + 1)
        )


def test_default_shape_and_values_remain_identical(store):
    seed(store, "same\nsame HIT\nHIT tail\nother\n")
    baseline = store.find_note("note.md", "HIT")
    assert set(baseline) == {"path", "exists", "version", "total_lines", "matches", "next_line"}
    assert store.find_note("note.md", "HIT", compact=False) == baseline
    assert store.find_note("note.md", "HIT", compact=False, context_before=0, context_after=0) == baseline


def test_compact_zero_context_represents_every_duplicate_hit_once(store):
    seed(store, "repeat HIT\nrepeat HIT\nrepeat HIT\n")
    default = store.find_note("note.md", "HIT")
    compact = store.find_note("note.md", "HIT", compact=True)
    assert compact["version"] == default["version"]
    assert compact["total_lines"] == default["total_lines"]
    assert compact["next_line"] == default["next_line"]
    assert compact["context_truncated"] is False
    assert [deref for deref in (dereference(compact, ref) for ref in compact["matches"])] == default["matches"]
    assert len(compact["windows"]) == 1
    assert compact["windows"][0]["lines"] == default["matches"]
    assert_compact_invariants(compact)


def test_compact_lines_preserve_bom_crlf_anchors_and_exact_match_text(store):
    content = "\ufeffleft\r\nMATCH\r\nright\r\nMATCH\r\n"
    seed(store, content)
    old = store.find_note("note.md", "MATCH")
    compact = store.find_note("note.md", "MATCH", compact=True)
    assert compact["version"] == old["version"]
    assert [dereference(compact, ref) for ref in compact["matches"]] == old["matches"]
    assert [line_number(entry) for entry in window_lines(compact)] == [2, 4]
    assert [entry.split("|", 1)[1] for entry in window_lines(compact)] == ["MATCH", "MATCH"]
    assert store.read_note("note.md")["content"] == content
    assert_compact_invariants(compact)


def test_compact_context_merges_overlapping_and_adjacent_windows(store):
    seed(store, "".join(f"row {n}" + (" HIT" if n in (4, 8, 10) else "") + "\n" for n in range(1, 15)))
    result = store.find_note("note.md", "HIT", context_before=2, context_after=2, compact=True)
    assert [n for n in map(line_number, window_lines(result))] == list(range(2, 13))
    assert len(result["windows"]) == 1
    assert [ref["line"] for ref in result["matches"]] == [4, 8, 10]
    assert [line_number(dereference(result, ref)) for ref in result["matches"]] == [4, 8, 10]
    assert not result["context_truncated"]
    assert_compact_invariants(result)


def test_compact_context_pagination_is_based_only_on_match_refs(store):
    seed(store, "".join(f"HIT row {n}\n" for n in range(1, 8)))
    seen, cursor = [], 1
    while cursor is not None:
        old = store.find_note("note.md", "HIT", start_line=cursor, max_matches=2)
        page = store.find_note("note.md", "HIT", start_line=cursor, max_matches=2,
                               context_before=1, context_after=1, compact=True)
        assert page["next_line"] == old["next_line"]
        assert [dereference(page, ref) for ref in page["matches"]] == old["matches"]
        assert [ref["line"] for ref in page["matches"]] == [line_number(entry) for entry in old["matches"]]
        seen.extend(ref["line"] for ref in page["matches"])
        cursor = page["next_line"]
        assert_compact_invariants(page)
    assert seen == list(range(1, 8))


@pytest.mark.parametrize("content", [None, "", "\ufeff", "plain\ntext\n"])
def test_compact_empty_or_no_match_has_no_windows(store, content):
    if content is not None:
        seed(store, content)
    result = store.find_note("note.md", "absent", compact=True)
    assert result["matches"] == []
    assert result["windows"] == []
    assert result["next_line"] is None
    assert result["context_truncated"] is False


def test_compact_long_adjacent_matches_keep_full_text_and_bound_the_page(store):
    chunk = "HIT" + "中" * 2000
    seed(store, "\n".join([chunk] * 3) + "\nend\n")
    old = store.find_note("note.md", "HIT", max_matches=10)
    page = store.find_note("note.md", "HIT", max_matches=10, compact=True, context_after=2)
    assert [ref["line"] for ref in page["matches"]] == [1, 2]
    assert page["next_line"] == old["next_line"] == 3
    assert [dereference(page, ref) for ref in page["matches"]] == old["matches"]
    assert page["next_line"] == 3
    assert [dereference(page, ref) for ref in page["matches"]] == [
        entry for entry in window_lines(page) if line_number(entry) in (1, 2)
    ]
    assert all(len(dereference(page, ref).split("|", 1)[1]) == len(chunk) for ref in page["matches"])
    assert sum(rendered_cost(entry) for entry in window_lines(page)) <= MAX_READ_BYTES
    assert page["context_truncated"]
    assert_compact_invariants(page)


def test_compact_longline_over_limit_keeps_existing_line_too_long_semantics(store):
    seed(store, "HIT" + "x" * (MAX_READ_BYTES + 1))
    with pytest.raises(VaultError) as caught:
        store.find_note("note.md", "HIT", compact=True)
    assert caught.value.code == "LINE_TOO_LONG"


def test_compact_oversized_nonmatch_neighbor_is_skipped_without_hiding_hit(store):
    seed(store, "x" * (MAX_READ_BYTES + 1) + "\nHIT\ntrailing\n")
    result = store.find_note("note.md", "HIT", context_before=1, context_after=1, compact=True)
    assert [ref["line"] for ref in result["matches"]] == [2]
    assert [line_number(dereference(result, ref)) for ref in result["matches"]] == [2]
    assert [line_number(line) for line in window_lines(result)] == [2, 3]
    assert result["context_truncated"]
    assert_compact_invariants(result)


def test_compact_context_hard_line_cap_reports_truncation_without_losing_hits(store):
    content = "\n".join("HIT" if index % 20 == 0 else f"context {index}" for index in range(1, 1000)) + "\n"
    seed(store, content)
    result = store.find_note("note.md", "HIT", max_matches=10, context_before=50, context_after=50, compact=True)
    assert len(result["matches"]) == 10
    assert all(dereference(result, ref).endswith("|HIT") for ref in result["matches"])
    assert result["context_truncated"]
    assert_compact_invariants(result)


@pytest.mark.parametrize("bad", [1, 0, "true", None, [], {}])
def test_compact_option_is_strict_bool(store, bad):
    seed(store, "HIT\n")
    with pytest.raises(VaultError) as caught:
        store.find_note("note.md", "HIT", compact=bad)
    assert caught.value.code == "INVALID_COMPACT"


@pytest.mark.parametrize("name,bad", [
    ("context_before", True), ("context_before", -1), ("context_before", 51),
    ("context_after", 1.0), ("context_after", "1"), ("context_after", None),
])
def test_compact_still_strictly_validates_context_distances(store, name, bad):
    seed(store, "HIT\n")
    with pytest.raises(VaultError) as caught:
        store.find_note("note.md", "HIT", compact=True, **{name: bad})
    assert caught.value.code == "INVALID_RANGE"


def test_compact_json_has_deduplicated_line_entries(store):
    repeated = "HIT " + "long-match-data " * 30
    seed(store, repeated + "\ncontext\n" + repeated + "\n")
    old = store.find_note("note.md", "HIT", context_before=1, context_after=1)
    compact = store.find_note("note.md", "HIT", context_before=1, context_after=1, compact=True)
    default_text = json.dumps(old, ensure_ascii=False, separators=(",", ":"))
    compact_text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    assert len(compact_text.encode("utf-8")) < len(default_text.encode("utf-8"))
    assert [dereference(compact, ref) for ref in compact["matches"]] == old["matches"]
