"""Conservative byte-preserving local Markdown link rebasing for note moves.

This is deliberately a small lexer, not a Markdown renderer. It edits only
destination spans it can identify and resolve uniquely; uncertain references
fail closed.
"""
from __future__ import annotations

import json
import posixpath
import re
import unicodedata
from urllib.parse import unquote_to_bytes, quote

from .store import VaultError

MAX_NOTE = 1024 * 1024
MAX_LINKS_PER_NOTE = 2000
MAX_LABEL_CHARS = 4096
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _escaped(text, offset):
    count = 0
    offset -= 1
    while offset >= 0 and text[offset] == "\\":
        count += 1
        offset -= 1
    return count % 2 == 1


def _reject_containers(text, path, mask):
    """Refuse containers whose Markdown/code boundaries we cannot prove.

    Top-level indented code remains protected. In lists or after an active
    paragraph the same indentation can instead be live Markdown, so refusing
    those link-bearing continuations is safer than silently skipping them.
    """
    offset = 0
    in_list = False
    paragraph = False
    raw_tags = r"(?:pre|code|script|style|textarea|div|table|details|section|article|iframe)"
    for line in text.splitlines(keepends=True):
        if offset == 0 and line.startswith("\ufeff"):
            line = line[1:]
            offset += 1
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        linkish = "[[" in line or re.search(r"\]\s*\(", line)
        marker = re.match(r" {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]+)", line)
        if indent and (in_list or paragraph) and linkish:
            _error("UNSUPPORTED_LINK", "List/paragraph continuation links require manual normalization before moving.", path, text, offset)
        protected = all(char.isspace() or mask[offset + index] for index, char in enumerate(line))
        if not protected:
            if re.match(r" {0,3}>", line):
                _error("UNSUPPORTED_LINK", "Blockquote link/code containers are unsupported for moves.", path, text, offset)
            if marker and re.search(r"`{3,}|~{3,}", line[marker.end():]):
                _error("UNSUPPORTED_LINK", "List code-fence containers are unsupported for moves.", path, text, offset)
            for raw_match in re.finditer(r"<\s*" + raw_tags + r"\b", line, re.I):
                if not mask[offset + raw_match.start()]:
                    _error("UNSUPPORTED_LINK", "Raw HTML/code blocks are unsupported for moves.", path, text, offset + raw_match.start())
            html_line = line[marker.end():] if marker else line.lstrip(" \t")
            opening = re.match(r"</?([A-Za-z][A-Za-z0-9-]*)(?=[ \t/>])", html_line)
            inline_tags = {"a", "span", "em", "strong", "b", "i", "u", "s", "del", "sup", "sub", "small", "mark", "img", "br", "input", "meta"}
            if opening and opening.group(1).lower() not in inline_tags:
                _error("UNSUPPORTED_LINK", "HTML block containers require manual normalization before moving.", path, text, offset)
            if re.match(r"<(?:\?|![A-Z]|!\[CDATA\[)", html_line):
                _error("UNSUPPORTED_LINK", "HTML processing/declaration blocks are unsupported for moves.", path, text, offset)
            if re.match(r" {0,3}</?[A-Za-z][^>]*>[ \t]*\r?\n?$", line):
                _error("UNSUPPORTED_LINK", "Raw HTML block boundaries are unsupported for moves.", path, text, offset)
        if not stripped.strip():
            paragraph = False
        elif marker:
            in_list = True
            paragraph = False
        elif indent == 0:
            in_list = False
            paragraph = not protected and not re.match(r"(?:#{1,6} |>|[-*_]{3,}\s*$)", stripped)
        offset += len(line)


def _indent_columns(prefix):
    columns = 0
    for char in prefix:
        columns += 4 - columns % 4 if char == "\t" else 1
    return columns


def _code_mask(text):
    """Mark fenced, indented, and inline code so prechecks ignore literals."""
    mask = [False] * len(text)
    i = 1 if text.startswith("\ufeff") else 0
    if i:
        mask[0] = True
    n = len(text); fence = None; flen = 0; line_start = True
    while i < n:
        if line_start:
            j = i
            while j<n and text[j] in " \t": j+=1
            columns = _indent_columns(text[i:j])
            if columns < 4 and j<n and text[j] in "`~":
                k=j
                while k<n and text[k]==text[j]: k+=1
                e=text.find("\n",k); e=n if e<0 else e
                tail=text[k:e].strip(" \t\r")
                valid_open=text[j]!="`" or "`" not in text[k:e]
                if k-j>=3 and (fence is not None or valid_open):
                    if fence is None: fence=text[j]; flen=k-j
                    elif text[j]==fence and k-j>=flen and not tail: fence=None
                    end=e if e==n else e+1
                    for x in range(i,end): mask[x]=True
                    i=end; line_start=True; continue
            if columns >= 4:
                e=text.find("\n",i); end=n if e<0 else e+1
                for x in range(i,end): mask[x]=True
                i=end; line_start=True; continue
        if fence:
            e=text.find("\n",i); end=n if e<0 else e+1
            for x in range(i,end): mask[x]=True
            i=end; line_start=True; continue
        if text[i]=="\n": i+=1; line_start=True; continue
        line_start=False
        if text[i]=='`' and not _escaped(text, i):
            k=i
            while k<n and text[k]=='`': k+=1
            marker=text[i:k]; end=k
            while end<n:
                end=text.find(marker,end)
                if end<0: break
                if (end==0 or text[end-1]!='`') and (end+len(marker)==n or text[end+len(marker)]!='`'): break
                end+=len(marker)
            if end>=0 and end<n:
                for x in range(i,end+len(marker)): mask[x]=True
                i=end+len(marker); continue
            for x in range(i,k): mask[x]=True
            i=k; continue
        i+=1
    return mask


def _error(code, msg, path, text, offset, candidates=None):
    line = text.count("\n", 0, offset) + 1
    details = {"path": path, "line": line}
    if candidates is not None:
        details["candidates"] = sorted(candidates)[:8]
    raise VaultError(code, msg, **details)


def _decode_url(s, path, text, off):
    if "?" in s or "\\" in s or "\x00" in s:
        _error("UNSUPPORTED_LINK", "Unsupported local link syntax.", path, text, off)
    try:
        raw = unquote_to_bytes(s)
        # Strict percent syntax and UTF-8, exactly one decode.
        if re.search(r"%(?![0-9A-Fa-f]{2})", s):
            raise ValueError
        decoded = raw.decode("utf-8", "strict")
    except (ValueError, UnicodeDecodeError):
        _error("UNSUPPORTED_LINK", "Malformed URL-encoded local link.", path, text, off)
    if ("\\" in decoded or "\x00" in decoded or decoded.startswith("/")
            or "?" in decoded or any(ord(c)<32 or ord(c)==127 for c in decoded)):
        _error("UNSUPPORTED_LINK", "Unsupported local link path.", path, text, off)
    if posixpath.normpath(posixpath.join(posixpath.dirname(path), decoded)) == ".." or posixpath.normpath(posixpath.join(posixpath.dirname(path), decoded)).startswith("../"):
        _error("UNSUPPORTED_LINK", "Local link escapes the vault root.", path, text, off)
    return decoded


def _resolve(target, note_path, catalog, wiki=False):
    """Return all catalog matches. Relative paths are anchored at note dir."""
    if not target:
        return set()
    rooted=wiki and target.startswith("/")
    if (target.startswith("/") and not rooted) or "\\" in target or "\x00" in target:
        return set()
    if rooted: target=target.lstrip("/")
    candidates = set()
    explicit = rooted or target.startswith(("./", "../"))
    if explicit:
        raw = posixpath.normpath(target) if rooted else posixpath.normpath(posixpath.join(posixpath.dirname(note_path), target))
        if raw not in ("..",) and not raw.startswith("../"):
            candidates.add(raw)
            candidates.add(raw + ".md")
    else:
        # Ordinary wiki names: exact vault-relative, note-relative, then unique basename.
        candidates.add(posixpath.normpath(target))
        if wiki: candidates.add(posixpath.normpath(target + ".md"))
        rel = posixpath.normpath(posixpath.join(posixpath.dirname(note_path), target))
        if rel not in ("..",) and not rel.startswith("../"):
            candidates.add(rel)
            if wiki: candidates.add(posixpath.normpath(rel + ".md"))
        if wiki:
            candidates.update(p for p in catalog if p == target or p.endswith("/" + target) or (p == target+".md" or p.endswith("/"+target+".md")))
        # Markdown paths are normally relative to the containing note.
        if not wiki:
            candidates = {rel} if rel not in ("..",) and not rel.startswith("../") else set()
            if candidates:
                candidates.add(rel + ".md")
    return candidates & catalog


def _split_fragment(value):
    if "#" in value:
        return value.split("#", 1)[0], "#" + value.split("#", 1)[1]
    return value, ""


def _markdown_label_close(text, opener):
    """Return (closing bracket, complex) for a Markdown label.

    A plain ``find(']')`` is unsafe: it can mistake a nested/escaped bracket
    for the label terminator and then silently miss a local link destination.
    We scan escapes and bracket balance, but let the caller reject complex
    labels only when they are followed by a link destination.
    """
    depth = 1
    complex_label = False
    i = opener + 1
    while i < len(text):
        if i - opener > MAX_LABEL_CHARS + 1:
            return -2, True
        ch = text[i]
        if ch == "\\":
            # Backslash escapes the next character in CommonMark.
            if i + 1 < len(text):
                if text[i + 1] in "[]":
                    complex_label = True
                i += 2
                continue
        elif ch == "[":
            depth += 1
            complex_label = True
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i, complex_label
        i += 1
    return -1, complex_label


def _tokens(text, path):
    """Yield (start,end,targetstart,targetend,kind, wiki, original) spans."""
    n = len(text); i = 1 if text.startswith("\ufeff") else 0; line_start = True; fence = None; fence_len = 0
    while i < n:
        if line_start:
            j = i
            while j < n and text[j] in " \t": j += 1
            columns = _indent_columns(text[i:j])
            if columns < 4 and j < n and text[j] in "`~":
                ch = text[j]; k = j
                while k < n and text[k] == ch: k += 1
                line_end=text.find('\n',k); line_end=n if line_end<0 else line_end
                rest=text[k:line_end].rstrip('\r')
                if k-j >= 3 and (ch != '`' or '`' not in text[k:line_end]):
                    if fence is None: fence, fence_len = ch, k-j
                    elif ch == fence and k-j >= fence_len and not rest.strip(" \t"): fence = None
                    end = text.find("\n", i); i = n if end < 0 else end+1; line_start=True; continue
            # Indented code blocks.
            if columns >= 4:
                end=text.find("\n",i); i=n if end<0 else end+1; line_start=True; continue
        if fence:
            end=text.find("\n",i); i=n if end<0 else end+1; line_start=True; continue
        if text[i] == "\n": i+=1; line_start=True; continue
        line_start=False
        # Inline code span: matching run length, variable backticks.
        if text[i] == '`' and not _escaped(text, i):
            k=i
            while k<n and text[k]=='`': k+=1
            marker=text[i:k]; end=k
            while end<n:
                end=text.find(marker,end)
                if end<0: break
                before=(end==0 or text[end-1]!='`')
                after=(end+len(marker)==n or text[end+len(marker)]!='`')
                if before and after: break
                end+=len(marker)
            if end>=0: i=end+len(marker); continue
            i=k; continue
        # Reject HTML tags/comments and YAML frontmatter conservatively.
        if text.startswith("<!--", i):
            end=text.find("-->",i+4)
            i=(end+3 if end>=0 else n); continue
        if text[i] == '<' and re.match(r"</?[A-Za-z][^>]*>", text[i:]):
            end=text.find(">",i)
            if end>=0:
                tag=text[i:end+1]
                for attr in re.finditer(r"\b(?:href|src)\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))",tag,re.I):
                    value=(attr.group(2) or attr.group(3) or "").strip()
                    if value and not value.startswith(("#","//")) and not _SCHEME.match(value):
                        _error("UNSUPPORTED_LINK","Local HTML reference may be affected by move.",path,text,i)
                i=end+1; continue
        # Escaped/nested inline constructions are not safe to reinterpret.
        if text[i:i+2] == r"\[" and re.search(r"\]\s*\(", text[i+2:text.find("\n", i) if "\n" in text[i:] else n]):
            _error("UNSUPPORTED_LINK", "Escaped Markdown link syntax is unsupported.", path, text, i)
        # Wiki link/embed. Skip escaped opener.
        emb = text.startswith("![[",i); op=i+1 if emb else i
        if text.startswith("[[",op) and not _escaped(text, i):
            close=text.find("]]",op+2)
            if close<0: _error("UNSUPPORTED_LINK","Unterminated wiki link.",path,text,i)
            body=text[op+2:close]
            if "[" in body or "]" in body or "\n" in body: _error("UNSUPPORTED_LINK","Nested or multiline wiki link.",path,text,i)
            raw=body.split("|",1)[0]
            if "\\" in raw or "?" in raw or "##" in raw or "^^" in raw:
                _error("UNSUPPORTED_LINK","Unsupported wiki search or escaped syntax.",path,text,i)
            stem,frag=_split_fragment(raw)
            ts=op+2; te=ts+len(stem)
            yield i,close+2,ts,te,"wiki",True,stem
            i=close+2; continue
        # Markdown inline link/image; parse balanced label and parenthesis, simple destination only.
        image=text.startswith("![",i); op=i+1 if image else i
        if text[op:op+1]=="[" and not _escaped(text, i):
            close, complex_label = _markdown_label_close(text, op)
            if close < 0:
                _error("UNSUPPORTED_LINK", "Unterminated or overlong Markdown bracket form.", path, text, i)
            if complex_label:
                _error("UNSUPPORTED_LINK", "Nested or escaped Markdown bracket forms are unsupported.", path, text, i)
            if close>=0 and close+1<n and text[close+1]=='(':
                if "\n" in text[op+1:close] or "\r" in text[op+1:close]: _error("UNSUPPORTED_LINK","Multiline Markdown link labels are unsupported.",path,text,i)
                j=close+2; depth=1
                while j<n and depth:
                    if text[j]=='(': depth+=1
                    elif text[j]==')': depth-=1
                    j+=1
                if depth: _error("UNSUPPORTED_LINK","Unterminated Markdown link.",path,text,i)
                inner=text[close+2:j-1]
                if "\n" in inner or "\r" in inner: _error("UNSUPPORTED_LINK","Multiline Markdown destinations are unsupported.",path,text,i)
                if inner.startswith("<") != inner.endswith(">"):
                    _error("UNSUPPORTED_LINK","Unbalanced angle delimiters in Markdown destination.",path,text,i)
                m=re.match(r"<?([^\s<>]+)>?(?:\s+(['\"]).*?\2)?$",inner)
                if not m: _error("UNSUPPORTED_LINK","Unsupported Markdown link form.",path,text,i)
                val=m.group(1); start=close+2+(1 if inner.startswith('<') else 0); end=start+len(val)
                yield i,j,start,end,"markdown",False,val
                i=j; continue
            i = close + 1
            continue
        i+=1


def rewrite_note_links(data: bytes, note_path: str, new_note_path: str, source: str,
                       destination: str, catalog: set[str], *, link_budget: list[int] | None = None, target_summary: dict | None = None) -> tuple[bytes, int]:
    """Rewrite resolvable inbound and moved-note outbound local references.

    ``catalog`` is the pre-move set of tracked paths. Returns original bytes
    with only destination token spans changed, plus changed-link count.
    """
    if len(data)>MAX_NOTE: raise VaultError("UNSUPPORTED_LINK","Note exceeds link lexer size limit.",path=note_path)
    try: text=data.decode("utf-8", "strict")
    except UnicodeDecodeError: raise VaultError("UNSUPPORTED_LINK","Notes containing non-UTF-8 bytes cannot be safely link-rewritten.",path=note_path)
    lone_cr = re.search(r"\r(?!\n)", text)
    if lone_cr:
        _error("UNSUPPORTED_LINK", "CR-only line endings are unsupported for move link scanning; use LF or CRLF.", note_path, text, lone_cr.start())
    bom=1 if text.startswith("\ufeff") else 0
    open_fm=re.match(r"---[ \t]*\r?\n",text[bom:])
    if open_fm:
        start=bom+open_fm.end()
        fm_end=re.search(r"(?m)^(?:---|\.\.\.)[ \t]*\r?$",text[start:])
        if not fm_end:
            _error("UNSUPPORTED_LINK","Unterminated frontmatter cannot be safely inspected for links.",note_path,text,bom)
        close=start+fm_end.end()
        fm=text[:close]
        if re.search(r"(?:\.\.?/|[^\s\"\'<>]+\.(?:md|markdown|png|jpe?g|gif|svg|webp|pdf)(?=[\s\"\'\]#,]|$))", fm, re.I):
            _error("UNSUPPORTED_LINK", "Frontmatter contains a possible local file reference.", note_path, text, bom)
        if re.search(r"\[\[|\]\s*\(|\b(?:href|src)\s*=",fm,re.I) or re.search(r"(?m)^\s*(?!aliases\s*:)[\w.-]+\s*:\s*(?:\.{1,2}/|[^\s]+\.(?:md|markdown|png|jpe?g|gif|svg|webp|pdf)(?:[#?].*)?)\s*$",fm,re.I):
            _error("UNSUPPORTED_LINK","Frontmatter may contain a local reference affected by move.",note_path,text,bom)
    mask=_code_mask(text)
    _reject_containers(text, note_path, mask)
    for special in re.finditer(r"%%|\$\$", text):
        if not mask[special.start()] and not _escaped(text, special.start()):
            _error("UNSUPPORTED_LINK", "Obsidian comment/math containers are unsupported for moves.", note_path, text, special.start())
    for math in re.finditer(r"(?<!\\)\$[^$\n]+\$", text):
        if not mask[math.start()] and ("[[" in math.group() or re.search(r"\]\s*\(", math.group())):
            _error("UNSUPPORTED_LINK", "Link-like text inside inline math is unsupported for moves.", note_path, text, math.start())
    for defs in re.finditer(r"(?m)^\s*\[[^\]\n]+\]:\s*\S+", text):
        if not mask[defs.start()]:
            _error("UNSUPPORTED_LINK", "Reference-style Markdown links are not supported for move rewriting.", note_path, text, defs.start())
    after=(catalog-{source})|{destination}
    edits=[]
    def reject_noncanonical_target(candidates, offset, wiki=False):
        """Reject case/NFC aliases of moved paths rather than miss a backlink."""
        moved=(source,destination)
        candidates = candidates + [value + ".md" for value in candidates]
        for candidate in candidates:
            norm=posixpath.normpath(candidate)
            if norm not in moved and any(_canonical_path_key(norm)==_canonical_path_key(p) for p in moved):
                _error("UNSUPPORTED_LINK","Case- or Unicode-normalized link spelling may refer to a moved note.",note_path,text,offset)
        if wiki:
            for candidate in candidates:
                name=posixpath.basename(candidate)
                if not any(name==posixpath.basename(p) for p in moved) and any(
                    _canonical_path_key(name)==_canonical_path_key(posixpath.basename(p)) for p in moved
                ):
                    _error("UNSUPPORTED_LINK","Case- or Unicode-normalized wiki name may refer to a moved note.",note_path,text,offset)
    token_count = 0
    for _,_,a,b,kind,wiki,val in _tokens(text,note_path):
        token_count += 1
        if token_count > MAX_LINKS_PER_NOTE or (link_budget is not None and link_budget[0] <= 0):
            _error("MOVE_TOO_LARGE", "Move link scan budget exceeded.", note_path, text, a)
        if link_budget is not None:
            link_budget[0] -= 1
        # Outbound references in moved source are rebased to preserve their old target.
        moving_out = note_path==source and new_note_path==destination
        if kind=="wiki":
            target,frag=_split_fragment(val)
            if not target: continue # pure anchor / block reference
            if _SCHEME.match(target) or target.startswith("//"): continue
            if target.startswith(("./", "../")):
                normalized=posixpath.normpath(posixpath.join(posixpath.dirname(note_path), target))
                if normalized==".." or normalized.startswith("../"):
                    _error("UNSUPPORTED_LINK","Wiki path escapes the vault root.",note_path,text,a)
            if target.startswith("/"):
                normalized=posixpath.normpath(target.lstrip("/"))
                if normalized==".." or normalized.startswith("../"):
                    _error("UNSUPPORTED_LINK","Rooted wiki path escapes the vault root.",note_path,text,a)
            candidates=[target]
            if target.startswith("/"):
                candidates.append(target.lstrip("/"))
            else:
                candidates.append(posixpath.join(posixpath.dirname(note_path),target))
            reject_noncanonical_target(candidates,a,wiki=True)
            # wiki paths can use relative explicit paths and fuzzy names.
            matches=_resolve(target,note_path,catalog,True)
            involves=source in matches
            if moving_out:
                if not matches:
                    if not target.startswith(("./","../","/")):
                        _error("AMBIGUOUS_LINK","Dangling outgoing wiki target cannot be safely rebased.",note_path,text,a)
                    intended=posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(note_path),target))
                    later=_resolve(_relative_link(new_note_path,intended,True),new_note_path,after,True)
                    if destination in later: _error("AMBIGUOUS_LINK","Move would make a dangling wiki link resolve to the moved note.",note_path,text,a,later)
                    replacement=_relative_link(new_note_path,intended,True)
                    edits.append((a,b,replacement+frag)); continue
                if len(matches)>1: _error("AMBIGUOUS_LINK","Wiki link resolves ambiguously.",note_path,text,a,matches)
                resolved=next(iter(matches))
                dest=destination if resolved==source else resolved
                newtarget=_relative_link(new_note_path,dest,True)
                verified=_resolve(newtarget,new_note_path,after,True)
                if verified!={dest}: _error("AMBIGUOUS_LINK","Rewritten wiki target did not resolve uniquely after move.",note_path,text,a,verified)
                # Preserve fragment; aliases remain outside target span.
                replacement=newtarget
            elif involves:
                if len(matches)!=1: _error("AMBIGUOUS_LINK","Wiki link may refer to moved note.",note_path,text,a,matches)
                replacement=_relative_link(note_path,destination,True)
                verified=_resolve(replacement,note_path,after,True)
                if verified!={destination}: _error("AMBIGUOUS_LINK","Rewritten wiki target did not resolve uniquely after move.",note_path,text,a,verified)
            else:
                # Guard: destination must not newly capture dangling/ambiguous ordinary wiki links.
                aftermatches=_resolve(target,note_path,after,True)
                if aftermatches != matches:
                    _error("AMBIGUOUS_LINK","Move would retarget an unrelated wiki link.",note_path,text,a,aftermatches)
                continue
            edits.append((a,b,replacement+frag))
        else:
            raw=val
            # CommonMark angle forms handled separately; external schemes untouched.
            if raw.startswith("//") or _SCHEME.match(raw): continue
            if re.search(r"&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]+);", raw, re.I):
                _error("UNSUPPORTED_LINK", "Entity-bearing local Markdown destinations are unsupported.", note_path, text, a)
            target,frag=_split_fragment(raw)
            if not target: continue
            decoded=_decode_url(target,note_path,text,a)
            reject_noncanonical_target([posixpath.join(posixpath.dirname(note_path),decoded)],a)
            matches=_resolve(decoded,note_path,catalog,False)
            moving_out=note_path==source and new_note_path==destination
            if moving_out:
                if not matches:
                    intended=posixpath.normpath(posixpath.join(posixpath.dirname(note_path),decoded))
                    if intended==".." or intended.startswith("../"):
                        _error("UNSUPPORTED_LINK","Outgoing link escapes the vault root.",note_path,text,a)
                    later=_resolve(_relative_link(new_note_path,intended,False),new_note_path,after,False)
                    if destination in later: _error("AMBIGUOUS_LINK","Move would make a dangling Markdown link resolve to the moved note.",note_path,text,a,later)
                    repl=quote(_relative_link(new_note_path,intended,False),safe="/.-_~")+frag
                    edits.append((a,b,repl)); continue
                if len(matches)>1: _error("AMBIGUOUS_LINK","Markdown link resolves ambiguously.",note_path,text,a,matches)
                resolved=next(iter(matches)); dest=destination if resolved==source else resolved
                repl=_relative_link(new_note_path,dest,False)
                verified=_resolve(repl,new_note_path,after,False)
                if verified!={dest}: _error("AMBIGUOUS_LINK","Rewritten Markdown target did not resolve uniquely after move.",note_path,text,a,verified)
                # encode spaces, preserve Unicode and slash; # fragment remains literal
                repl=quote(repl,safe="/.-_~")+frag
                edits.append((a,b,repl))
            elif source in matches:
                if len(matches)!=1: _error("AMBIGUOUS_LINK","Markdown link may refer to moved note.",note_path,text,a,matches)
                newpath=_relative_link(note_path,destination,False)
                verified=_resolve(newpath,note_path,after,False)
                if verified!={destination}: _error("AMBIGUOUS_LINK","Rewritten Markdown target did not resolve uniquely after move.",note_path,text,a,verified)
                repl=quote(newpath,safe="/.-_~")+frag
                edits.append((a,b,repl))
            else:
                aftermatches=_resolve(decoded,note_path,after,False)
                if aftermatches != matches:
                    _error("AMBIGUOUS_LINK","Move would retarget an unrelated Markdown link.",note_path,text,a,aftermatches)
    if target_summary is not None:
        for a, b, repl in edits:
            if text[a:b] == repl:
                continue
            item = {"path": note_path, "result_path": new_note_path, "line": text.count("\n", 0, a) + 1,
                    "old_target": text[a:b], "new_target": repl}
            size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2
            if len(target_summary["items"]) < 50 and size <= target_summary["remaining_bytes"]:
                target_summary["items"].append(item)
                target_summary["remaining_bytes"] -= size
            else:
                target_summary["truncated"] = True
    out=text
    for a,b,repl in reversed(edits): out=out[:a]+repl+out[b:]
    return out.encode("utf-8"),sum(1 for a,b,repl in edits if text[a:b]!=repl)


def _relative_link(note_path, dest, wiki):
    rel=posixpath.relpath(dest,posixpath.dirname(note_path) or ".")
    if not rel.startswith(("./","../")): rel="./"+rel
    return rel


def _canonical_path_key(value):
    return unicodedata.normalize("NFC", value).casefold()
