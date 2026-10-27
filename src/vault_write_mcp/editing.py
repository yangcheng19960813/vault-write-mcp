"""Bounded line reads and strict edits against one immutable note snapshot."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .store import VaultError


MAX_READ_BYTES = 16384
MAX_EDITS = 100
MAX_CONTEXT_LINES = 200
MAX_CONTEXT_DISTANCE = 50


class LineEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["SWAP", "DEL", "INS.PRE", "INS.POST"]
    anchor: str
    end_anchor: str | None = None
    lines: list[str] | None = None


def validate_edits(edits: list[dict]) -> list[dict]:
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise VaultError("INVALID_PATCH", f"Supply 1–{MAX_EDITS} edits.")
    try:
        normalized = [LineEdit.model_validate(edit).model_dump(exclude_none=True) for edit in edits]
    except ValidationError as exc:
        raise VaultError("INVALID_PATCH", "Use op, anchor, optional end_anchor, and lines; unknown fields are rejected.") from exc
    for edit in normalized:
        lines = edit.get("lines")
        if edit["op"] == "DEL":
            if lines is not None:
                raise VaultError("INVALID_PATCH", "DEL must not include lines.")
        elif not lines or any("\r" in line or "\n" in line for line in lines):
            raise VaultError("INVALID_PATCH", "Supply a nonempty list of lines without CR or LF; use an empty string for a blank line.")
        if edit["op"].startswith("INS.") and "end_anchor" in edit:
            raise VaultError("INVALID_PATCH", "Insertions use one anchor, without end_anchor.")
    return normalized


def split_note(data: bytes) -> tuple[str, list[str]]:
    text = data.decode("utf-8")
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    text = text[len(bom):]
    # Only LF/CRLF delimit lines; Unicode separators inside Markdown stay intact.
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return bom, lines


def ending(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""


def anchor_for(number: int, line: str) -> str:
    digest = hashlib.sha256(line.encode("utf-8")).hexdigest()[:12]
    return f"{number}:{digest}"


def render(number: int, line: str) -> str:
    suffix = ending(line)
    content = line[:-len(suffix)] if suffix else line
    return f"{anchor_for(number, line)}|{content}"


def positive_int(name: str, value: int, maximum: int | None = None) -> None:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise VaultError("INVALID_RANGE", f"{name} must be a positive integer" + (f" no greater than {maximum}." if maximum else "."))


def bounded_lines(lines: list[str], indices, limit: int) -> tuple[list[str], int | None]:
    result = []
    size = 0
    for index in indices:
        entry = render(index + 1, lines[index])
        cost = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 2
        if len(result) == limit or size + cost > MAX_READ_BYTES:
            if not result:
                raise VaultError("LINE_TOO_LONG", "This line exceeds the bounded read limit; use read_note for this note.", line=index + 1)
            return result, int(result[-1].split(":", 1)[0]) + 1
        result.append(entry)
        size += cost
    return result, None


def read_range(data: bytes | None, start_line: int, max_lines: int) -> dict:
    positive_int("start_line", start_line)
    positive_int("max_lines", max_lines, 200)
    _, lines = split_note(data or b"")
    selected, next_line = bounded_lines(lines, range(start_line - 1, len(lines)), max_lines)
    return {"total_lines": len(lines), "start_line": start_line, "lines": selected, "next_line": next_line}


def validate_context(context_before: int, context_after: int) -> None:
    for name, value in (("context_before", context_before), ("context_after", context_after)):
        if type(value) is not int or not 0 <= value <= MAX_CONTEXT_DISTANCE:
            raise VaultError("INVALID_RANGE", f"{name} must be an integer from 0 to {MAX_CONTEXT_DISTANCE}.")


def rendered_cost(entry: str) -> int:
    # Same conservative JSON-array accounting as bounded_lines. Metadata is
    # outside this rendered-lines budget; duplicated matches are charged twice.
    return len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 2


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
        else:
            merged.append((start, stop))
    return merged


def context_windows(lines: list[str], spans: list[tuple[int, int]], before: int, after: int,
                    byte_budget: int = MAX_READ_BYTES) -> tuple[list[dict], bool]:
    if not lines or not spans:
        return [], False
    # A deletion has an empty output range: expose its surviving boundary (or
    # the preceding last line at EOF) so the next edit can use a fresh anchor.
    cores = merge_spans([(min(start, len(lines) - 1), min(max(stop, start + 1), len(lines))) for start, stop in spans])
    requested = merge_spans([(max(0, start - before), min(len(lines), stop + after)) for start, stop in cores])
    requested_count = sum(stop - start for start, stop in requested)
    selected = {}
    remaining = byte_budget
    # Prioritize changed/matched lines over optional surrounding text. Bounds
    # can omit even a core line; callers still get a successful bounded result.
    for groups in (cores, requested):
        for start, stop in groups:
            for index in range(start, stop):
                if index in selected:
                    continue
                if len(selected) == MAX_CONTEXT_LINES:
                    break
                entry = render(index + 1, lines[index])
                cost = rendered_cost(entry)
                if cost <= remaining:
                    selected[index] = entry
                    remaining -= cost
            if len(selected) == MAX_CONTEXT_LINES:
                break
    windows = []
    for index in sorted(selected):
        if windows and index + 1 == windows[-1]["end_line"] + 1:
            windows[-1]["end_line"] = index + 1
            windows[-1]["lines"].append(selected[index])
        else:
            windows.append({"start_line": index + 1, "end_line": index + 1, "lines": [selected[index]]})
    return windows, len(selected) != requested_count


def compact_find_windows(lines: list[str], hit_indices: list[int], before: int, after: int) -> tuple[list[dict], list[dict], bool]:
    """Render matched/context lines once and point matches into those windows.

    Hit lines are selected first, so every match which fit the existing match
    page remains represented even when optional context cannot fit the shared
    16 KiB/200-line result budget.
    """
    if not hit_indices:
        return [], [], False
    cores = merge_spans([(index, index + 1) for index in hit_indices])
    requested = merge_spans([(max(0, start - before), min(len(lines), stop + after))
                             for start, stop in cores])
    requested_count = sum(stop - start for start, stop in requested)
    selected: dict[int, str] = {}
    remaining = MAX_READ_BYTES
    for groups in (cores, requested):
        for start, stop in groups:
            for index in range(start, stop):
                if index in selected:
                    continue
                if len(selected) >= MAX_CONTEXT_LINES:
                    break
                entry = render(index + 1, lines[index])
                cost = rendered_cost(entry)
                if cost <= remaining:
                    selected[index] = entry
                    remaining -= cost
            if len(selected) >= MAX_CONTEXT_LINES:
                break

    windows: list[dict] = []
    references: dict[int, tuple[int, int]] = {}
    for index in sorted(selected):
        if windows and index == windows[-1]["end_line"]:
            window_index = len(windows) - 1
            line_index = len(windows[window_index]["lines"])
            windows[window_index]["end_line"] = index + 1
            windows[window_index]["lines"].append(selected[index])
        else:
            window_index = len(windows)
            line_index = 0
            windows.append({"start_line": index + 1, "end_line": index + 1, "lines": [selected[index]]})
        references[index] = (window_index, line_index)

    # All hits are guaranteed to be present: bounded_lines has already applied
    # the legacy per-page byte budget to them before this helper is called.
    matches = [
        {"line": index + 1, "window_index": references[index][0], "line_index": references[index][1]}
        for index in hit_indices
    ]
    return windows, matches, len(selected) != requested_count


def find_lines(data: bytes | None, text: str, start_line: int, max_matches: int,
               context_before: int = 0, context_after: int = 0, compact: bool = False) -> dict:
    positive_int("start_line", start_line)
    positive_int("max_matches", max_matches, 50)
    validate_context(context_before, context_after)
    if type(compact) is not bool:
        raise VaultError("INVALID_COMPACT", "compact must be a boolean.")
    if not isinstance(text, str) or not 1 <= len(text) <= 256 or "\n" in text or "\r" in text:
        raise VaultError("INVALID_QUERY", "text must contain 1–256 characters without CR or LF; matching is literal and case-sensitive.")
    _, lines = split_note(data or b"")
    indices = (index for index in range(start_line - 1, len(lines)) if text in lines[index])
    matches, next_line = bounded_lines(lines, indices, max_matches)
    result = {"total_lines": len(lines), "matches": matches, "next_line": next_line}
    if compact:
        hits = [int(entry.split(":", 1)[0]) - 1 for entry in matches]
        windows, match_refs, truncated = compact_find_windows(lines, hits, context_before, context_after)
        result.update({"matches": match_refs, "windows": windows, "context_truncated": truncated})
    elif context_before or context_after:
        hits = [int(entry.split(":", 1)[0]) - 1 for entry in matches]
        budget = MAX_READ_BYTES - sum(rendered_cost(entry) for entry in matches)
        windows, truncated = context_windows(lines, [(index, index + 1) for index in hits], context_before, context_after, budget)
        result.update({"windows": windows, "context_truncated": truncated})
    return result


def patch_context(data: bytes, spans: list[tuple[int, int]], before: int, after: int) -> dict:
    _, lines = split_note(data)
    windows, truncated = context_windows(lines, spans, before, after)
    return {"total_lines": len(lines), "windows": windows, "truncated": truncated}


def apply_patch(data: bytes, edits: list[dict]) -> bytes:
    return apply_patch_with_spans(data, edits)[0]


def apply_patch_with_spans(data: bytes, edits: list[dict]) -> tuple[bytes, list[tuple[int, int]]]:
    bom, original = split_note(data)
    newline = next((ending(line) for line in original if ending(line)), "\n")

    def locate(anchor: str) -> int:
        if not re.fullmatch(r"[1-9][0-9]{0,9}:[0-9a-f]{12}", anchor):
            raise VaultError("INVALID_ANCHOR", "Copy an anchor in line:hash format from read_note_range or find_note.")
        index = int(anchor.split(":", 1)[0]) - 1
        if index >= len(original) or anchor_for(index + 1, original[index]) != anchor:
            raise VaultError("ANCHOR_CONFLICT", "The anchor does not match this snapshot; re-read the affected lines.", anchor=anchor)
        return index

    plans = []
    for edit in edits:
        start = locate(edit["anchor"])
        end = locate(edit.get("end_anchor", edit["anchor"]))
        if end < start:
            raise VaultError("INVALID_PATCH", "end_anchor must not precede anchor.")
        op = edit["op"]
        eol = ending(original[start]) or newline
        supplied = edit.get("lines", [])
        if op == "DEL":
            replacement = []
        elif op == "SWAP":
            replacement = [line + eol for line in supplied[:-1]] + [supplied[-1] + ending(original[end])]
        elif op == "INS.PRE":
            replacement = [line + eol for line in supplied] + [original[start]]
        else:
            # Inserting after an unterminated last line needs a separator, but
            # retains the original absence of a final newline.
            suffix = ending(original[start])
            replacement = [original[start] if suffix else original[start] + eol]
            replacement += [line + eol for line in supplied[:-1]] + [supplied[-1] + suffix]
        plans.append((start, end + 1, replacement))

    plans.sort(key=lambda plan: plan[0])
    result = []
    spans = []
    cursor = 0
    for start, stop, replacement in plans:
        if start < cursor:
            raise VaultError("OVERLAPPING_EDITS", "Each edit must use a separate original line or range, including insertion anchors.")
        result.extend(original[cursor:start])
        spans.append((len(result), len(result) + len(replacement)))
        result.extend(replacement)
        cursor = stop
    result.extend(original[cursor:])
    return (bom + "".join(result)).encode("utf-8"), spans
