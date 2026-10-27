import pytest

from vault_write_mcp.links import rewrite_note_links
from vault_write_mcp.store import VaultError


def rewrite(s, path, new, src, dst, catalog):
    return rewrite_note_links(s.encode(), path, new, src, dst, set(catalog))


def test_inbound_wiki_alias_heading_block_and_embed():
    result, count = rewrite("[[old.md#heading|Name]] ![[old.md#^block]] [[old.md]]", "folder/ref.md", "folder/ref.md", "old.md", "archive/old.md", {"old.md", "folder/ref.md"})
    assert result.decode() == "[[../archive/old.md#heading|Name]] ![[../archive/old.md#^block]] [[../archive/old.md]]"
    assert count == 3


def test_inbound_markdown_unicode_and_spaces():
    result, count = rewrite("[café](old%20file.md#part) ![im](old%20file.md)", "d/ref.md", "d/ref.md", "d/old file.md", "d/new file.md", {"d/old file.md", "d/ref.md"})
    assert result.decode() == "[café](./new%20file.md#part) ![im](./new%20file.md)"
    assert count == 2


def test_moved_note_rebases_outgoing_relative_note_and_asset():
    result, count = rewrite("[n](../notes/n.md) ![a](../img/a%20b.png) [[../notes/n.md#h]]", "a/source.md", "moved/source.md", "a/source.md", "moved/source.md", {"a/source.md", "notes/n.md", "img/a b.png"})
    assert result.decode() == "[n](../notes/n.md) ![a](../img/a%20b.png) [[../notes/n.md#h]]"
    assert count == 0  # already-correct relative spellings need no byte rewrite


def test_code_external_and_pure_anchor_are_byte_preserved():
    text = "`[x](old.md)`\n```md\n[[old.md]]\n```\n[x](https://x.test/old.md) [a](#local)\n[[target]]"
    result, count = rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "target", "ref.md"})
    assert result.decode() == text and count == 0


def test_ambiguous_duplicate_wiki_fails_closed():
    with pytest.raises(VaultError) as e:
        rewrite("[[note]]", "ref.md", "ref.md", "x/note.md", "z/note.md", {"x/note.md", "y/note.md", "ref.md"})
    assert e.value.code == "AMBIGUOUS_LINK"
    assert e.value.details["line"] == 1


def test_destination_induced_wiki_binding_fails_closed():
    with pytest.raises(VaultError) as e:
        rewrite("[[new.md]]", "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert e.value.code == "AMBIGUOUS_LINK"


@pytest.mark.parametrize("url", ["old%ZZ.md", "old%FF.md", "old%5C.md", "../..%2Fescape.md"])
def test_malformed_unsupported_url(url):
    with pytest.raises(VaultError) as e:
        rewrite(f"[x]({url})", "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert e.value.code == "UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", ["[x][ref]\n\n[ref]: old.md", "<a href='old.md'>x</a>", "---\ncover: old.md\n---\ntext"])
def test_unsupported_potential_references(text):
    with pytest.raises(VaultError) as e:
        rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert e.value.code == "UNSUPPORTED_LINK"


def test_unrelated_link_and_destination_nonbinding_are_noop():
    text="[x](other.md) [[missing]]"
    got,n=rewrite(text,"d/ref.md","d/ref.md","old.md","new.md",{"other.md","d/ref.md"})
    assert got.decode()==text and n==0


def test_outgoing_markdown_relative_rebased_from_moved_folder():
    got,n=rewrite("[n](../target.md#h)","a/source.md","b/c/source.md","a/source.md","b/c/source.md",{"a/source.md","target.md"})
    assert got.decode()=="[n](../../target.md#h)" and n==1


def test_outgoing_dangling_markdown_target_rebased_by_absolute_intent():
    got,n=rewrite("[n](../not-created.md)","a/source.md","moved/deep/source.md","a/source.md","moved/deep/source.md",{"a/source.md"})
    assert got.decode()=="[n](../../not-created.md)" and n==1


def test_outgoing_dangling_explicit_wiki_target_is_rebased():
    got,n=rewrite("[[../not-created]]","a/source.md","moved/deep/source.md","a/source.md","moved/deep/source.md",{"a/source.md"})
    assert got.decode()=="[[../../not-created]]" and n==1


def test_explicit_wiki_md_extension_resolution_and_suffix():
    got,n=rewrite("[[./sub/note]] [[folder/note.md]]","a/ref.md","a/ref.md","folder/note.md","new/note.md",{"a/ref.md","a/sub/note.md","folder/note.md"})
    assert got.decode()=="[[./sub/note]] [[../new/note.md]]" and n==1


def test_unrelated_wiki_unique_becoming_ambiguous_is_rejected():
    with pytest.raises(VaultError) as e:
        rewrite("[[note]]","ref.md","ref.md","x/old.md","y/note.md",{"ref.md","x/note.md"})
    assert e.value.code=="AMBIGUOUS_LINK"


def test_code_reference_definition_is_not_rejected_and_exact_backtick_runs():
    text="`` ` [x][r] ``\n```md\n[r]: old.md\n```\n`[x](old.md)`"
    got,n=rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert got.decode()==text and n==0


@pytest.mark.parametrize("text", [
    "[outer [inner]](old.md)",
    r"[label\] tail](old.md)",
])
def test_nested_or_escaped_markdown_labels_fail_closed(text):
    with pytest.raises(VaultError) as e:
        rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert e.value.code == "UNSUPPORTED_LINK"


def test_reference_definition_after_code_is_still_checked():
    text="```md\n[hidden]: old.md\n```\n[visible]: old.md"
    with pytest.raises(VaultError) as e:
        rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert e.value.code == "UNSUPPORTED_LINK"


def test_invalid_backtick_fence_opener_does_not_hide_link():
    text="```lang`\n[x](old.md)"
    got,n=rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert got.decode()=="```lang`\n[x](./new.md)" and n==1


def test_fence_closer_with_trailing_text_does_not_unmask_links():
    text="```md\n[x](old.md)\n``` not-a-closer\n[[old.md]]\n```\n"
    got,n=rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert got.decode()==text and n==0


def test_frontmatter_ellipsis_closer_and_unterminated_rejection():
    text="---\naliases: [A]\n...\nbody"
    assert rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})==(text.encode(),0)
    with pytest.raises(VaultError) as e:
        rewrite("---\naliases: [A]\nbody","ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert e.value.code=="UNSUPPORTED_LINK"


def test_rooted_wiki_reference_that_escapes_vault_is_rejected():
    with pytest.raises(VaultError) as e:
        rewrite("[[/../../old.md]]","ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert e.value.code=="UNSUPPORTED_LINK"


@pytest.mark.parametrize(("text","source"), [
    ("[x](folder/old.md)","Folder/Old.md"),
    ("[[cafe\u0301.md]]","caf\u00e9.md"),
])
def test_case_or_unicode_normalized_potential_backlinks_fail_closed(text, source):
    with pytest.raises(VaultError) as e:
        rewrite(text,"ref.md","ref.md",source,"moved.md",{"ref.md",source})
    assert e.value.code=="UNSUPPORTED_LINK"


def test_frontmatter_bom_alias_is_ok_but_local_link_property_rejected():
    text="\ufeff---\naliases: [A, B]\n---\nbody"
    assert rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})==(text.encode(),0)
    with pytest.raises(VaultError):
        rewrite("\ufeff---\ncover: ./old.png\n---\n","ref.md","ref.md","old.png","new.png",{"ref.md","old.png"})


def test_external_html_and_fragment_html_unchanged_local_html_rejected():
    text='<a href="https://x.test">x</a> <img src="#frag">'
    assert rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})==(text.encode(),0)
    with pytest.raises(VaultError):
        rewrite('<img src="old.png">',"ref.md","ref.md","old.png","new.png",{"ref.md","old.png"})


def test_all_html_reference_attributes_are_inspected():
    text='<img src="https://example.test/a.png" href="old.png">'
    with pytest.raises(VaultError) as e:
        rewrite(text,"ref.md","ref.md","old.png","new.png",{"ref.md","old.png"})
    assert e.value.code=="UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", ["[x](a\nb)", "[x](<a)", "[[foo\\bar]]", "[[foo?bar]]", "[[foo##search]]", "[x](old%3Ffile.md)"])
def test_unsupported_complex_references_fail_closed(text):
    with pytest.raises(VaultError) as e:
        rewrite(text,"ref.md","ref.md","old.md","new.md",{"ref.md","old.md"})
    assert e.value.code=="UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", ["[multi\nline](old.md)", "[" * 5000 + "]" * 5000 + "(old.md)", "unfinished [label"])
def test_multiline_or_unbounded_bracket_forms_fail_closed(text):
    with pytest.raises(VaultError) as caught:
        rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"ref.md", "old.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", ["[[OLD]]", "[[./OLD]]"])
def test_extensionless_case_variant_cannot_silently_miss_backlink(text):
    with pytest.raises(VaultError) as caught:
        rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"ref.md", "old.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"


def test_link_scan_budget_is_shared_and_bounded():
    from vault_write_mcp.links import rewrite_note_links
    budget = [1]
    args = (b"[[old]]", "ref.md", "ref.md", "old.md", "new.md", {"ref.md", "old.md"})
    assert rewrite_note_links(*args, link_budget=budget)[1] == 1
    with pytest.raises(VaultError) as caught:
        rewrite_note_links(*args, link_budget=budget)
    assert caught.value.code == "MOVE_TOO_LARGE"


def test_nested_nonlink_brackets_are_rejected_before_quadratic_rescanning():
    with pytest.raises(VaultError) as caught:
        rewrite("[" * 2000 + "]" * 2000, "ref.md", "ref.md", "old.md", "new.md", {"ref.md", "old.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", [
    "> ~~~\n> [x](old.md)\n> ~~~\n",
    "<pre>\n[x](old.md)\n</pre>\n",
    "<code>[x](old.md)</code>",
    "- item\n    [x](old.md)\n",
    "Paragraph\n    [x](old.md)\n",
    "- ~~~\n  [x](old.md)\n  ~~~\n",
    "---\ncover: \"../assets/image.png\"\n---\n",
    "---\nrelated:\n  - ../other.md\n---\n",
    "[x](old&#46;md)",
])
def test_unsafe_containers_frontmatter_and_entities_fail_during_preview_scan(text):
    with pytest.raises(VaultError) as caught:
        rewrite(text, "old.md", "new.md", "old.md", "new.md", {"old.md", "other.md", "assets/image.png"})
    assert caught.value.code == "UNSUPPORTED_LINK"


def test_escaped_backticks_do_not_hide_an_active_markdown_reference():
    text = r"\`[x](old.md)\`"
    result, count = rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert result.decode() == r"\`[x](./new.md)\`" and count == 1


def test_top_level_indented_code_stays_protected_after_blank_line():
    text = "Paragraph\n\n    [x](old.md)\n    [[old]]\n"
    result, count = rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert result.decode() == text and count == 0


def test_obsidian_extensionless_markdown_notes_are_rewritten():
    result, count = rewrite("[x](old#heading)", "ref.md", "ref.md", "old.md", "folder/new.md", {"old.md", "ref.md"})
    assert result.decode() == "[x](./folder/new.md#heading)" and count == 1


def test_extensionless_markdown_new_binding_and_file_note_ambiguity_are_rejected():
    for text, catalog in (("[x](new)", {"ref.md", "old.md"}), ("[x](old)", {"ref.md", "old.md", "old"})):
        with pytest.raises(VaultError) as caught:
            rewrite(text, "ref.md", "ref.md", "old.md", "new.md", catalog)
        assert caught.value.code == "AMBIGUOUS_LINK"


def test_inline_code_prefix_cannot_hide_a_later_raw_html_block():
    with pytest.raises(VaultError) as caught:
        rewrite("`example` <code>[x](old.md)</code>", "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"
    literal = "`<code>[x](old.md)</code>`"
    assert rewrite(literal, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"}) == (literal.encode(), 0)


def test_utf8_bom_does_not_disable_first_line_fence_or_container_guard():
    code = "\ufeff~~~\r\n[x](old.md)\r\n~~~\r\n"
    assert rewrite(code, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"}) == (code.encode(), 0)
    with pytest.raises(VaultError) as caught:
        rewrite("\ufeff> ~~~\n> [x](old.md)\n> ~~~\n", "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", [
    "> paragraph\n    [x](old.md)\n",
    "<p>text [x](old.md)\n",
    "<p>text\n[x](old.md)\n",
    "</p>\n[x](old.md)\n",
    "- <p>text [x](old.md)\n",
    "<?processing\n[x](old.md)\n?>\n",
    "<![CDATA[\n[x](old.md)\n]]>\n",
])
def test_all_unsupported_block_container_starts_are_rejected_conservatively(text):
    with pytest.raises(VaultError) as caught:
        rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", [
    "<img src=\"https://example.test/image.png\">\n[x](old.md)\n",
    "<hr>text [x](old.md)\n",
    "<link href=\"https://example.test\">text [x](old.md)\n",
    "%% [x](old.md) %%\n",
    "$$\n[x](old.md)\n$$\n",
    "$[x](old.md)$\n",
])
def test_raw_html_void_blocks_and_obsidian_literal_containers_fail_closed(text):
    with pytest.raises(VaultError) as caught:
        rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"


@pytest.mark.parametrize(("target", "path"), [("./old", "ref.md"), ("../old", "d/ref.md")])
def test_explicit_relative_extensionless_markdown_backlinks_are_resolved(target, path):
    result, count = rewrite(f"[x]({target}#h)", path, path, "old.md", "new.md", {"old.md", path})
    expected = "./new.md" if path == "ref.md" else "../new.md"
    assert result.decode() == f"[x]({expected}#h)" and count == 1


def test_explicit_extensionless_outgoing_and_destination_capture_guards():
    result, count = rewrite("[x](../target)", "a/old.md", "deep/folder/new.md", "a/old.md", "deep/folder/new.md", {"a/old.md", "target.md"})
    assert result.decode() == "[x](../../target.md)" and count == 1
    for text, catalog in (("[x](./new)", {"old.md", "ref.md"}), ("[x](./old)", {"old.md", "old", "ref.md"})):
        with pytest.raises(VaultError) as caught:
            rewrite(text, "ref.md", "ref.md", "old.md", "new.md", catalog)
        assert caught.value.code == "AMBIGUOUS_LINK"


def test_cr_only_markdown_is_rejected_instead_of_silently_losing_backlinks():
    with pytest.raises(VaultError) as caught:
        rewrite("~~~\rcode\r~~~\r[x](old.md)\r", "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"


@pytest.mark.parametrize("text", ["[[old.v1]]", "[[./old.v1]]", "[x](old.v1)", "[x](./old.v1)"])
def test_optional_md_extension_works_for_dotted_note_basenames(text):
    result, count = rewrite(text, "ref.md", "ref.md", "old.v1.md", "new.v2.md", {"old.v1.md", "ref.md"})
    assert "./new.v2.md" in result.decode() and count == 1


def test_asset_and_optional_md_note_are_not_guessed_when_both_exist():
    with pytest.raises(VaultError) as caught:
        rewrite("[x](old.v1)", "ref.md", "ref.md", "old.v1.md", "new.md", {"old.v1", "old.v1.md", "ref.md"})
    assert caught.value.code == "AMBIGUOUS_LINK"


@pytest.mark.parametrize("indent", ["\t ", " \t", "\t\t", "  \t"])
def test_mixed_tab_indentation_uses_markdown_columns_for_code_protection(indent):
    text = f"{indent}[x](old.md)\n{indent}[[old]]\n"
    assert rewrite(text, "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"}) == (text.encode(), 0)


def test_label_budget_is_exactly_4096_content_characters():
    label = "x" * 4096
    result, count = rewrite(f"[{label}](old.md)", "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert result.decode() == f"[{label}](./new.md)" and count == 1
    with pytest.raises(VaultError) as caught:
        rewrite(f"[{label}x](old.md)", "ref.md", "ref.md", "old.md", "new.md", {"old.md", "ref.md"})
    assert caught.value.code == "UNSUPPORTED_LINK"
