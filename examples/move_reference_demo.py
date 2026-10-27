"""Exercise a previewed note move and local reference updates in a temp vault."""

import json
import tempfile

from vault_write_mcp.store import VaultStore, init_vault


def seed(store, path, content, request_id):
    return store.write_note(path, content, "missing", request_id, "move-demo")


def main():
    source = "guide/v1/old-title.md"
    destination = "archive/reference/new-title.md"
    with tempfile.TemporaryDirectory(prefix="vault-write-move-demo-") as directory:
        store = VaultStore(init_vault(directory))
        seed(store, "guide/shared.md", "# Shared\n", "seed-shared")
        seed(store, "policy.md", "# Policy\n", "seed-policy")
        seed(
            store,
            source,
            "# Old guide\n\n"
            "See [[../shared.md#setup|shared guide]].\n"
            "Read [policy](../../policy.md#scope).\n",
            "seed-source",
        )
        seed(
            store,
            "home/index.md",
            "Use [[../guide/v1/old-title.md#overview|the old guide]].\n",
            "seed-wiki-referrer",
        )
        seed(
            store,
            "start.md",
            "[Old guide](guide/v1/old-title.md#overview)\n",
            "seed-markdown-referrer",
        )

        preview = store.preview_move_note(source, destination)
        result = store.move_note(
            source,
            destination,
            preview["expected_versions"],
            preview["plan_version"],
            "move-old-title",
            "move-demo",
        )
        # A retry with exactly the same arguments replays the durable receipt.
        replay = store.move_note(
            source,
            destination,
            preview["expected_versions"],
            preview["plan_version"],
            "move-old-title",
            "move-demo",
        )

        moved_content = store.read_note(destination)["content"]
        wiki_referrer = store.read_note("home/index.md")["content"]
        markdown_referrer = store.read_note("start.md")["content"]
        assert store.read_note(source)["version"] == "missing"
        assert "[[../../guide/shared.md#setup|shared guide]]" in moved_content
        assert "[policy](../../policy.md#scope)" in moved_content
        assert "[[../archive/reference/new-title.md#overview|the old guide]]" in wiki_referrer
        assert "[Old guide](./archive/reference/new-title.md#overview)" in markdown_referrer
        assert result["rewritten_links"] == 3
        assert preview["rewritten_links"] == len(preview["link_changes"]) == 3
        assert preview["link_changes_truncated"] is False
        assert any(
            item["path"] == source
            and item["result_path"] == destination
            and item["old_target"] == "../shared.md"
            and item["new_target"] == "../../guide/shared.md"
            for item in preview["link_changes"]
        )
        assert replay["replayed"] is True
        assert replay["commit"] == result["commit"]

        print(
            json.dumps(
                {
                    "preview": preview,
                    "move": result,
                    "replayed": replay["replayed"],
                    "moved_note": moved_content,
                    "wiki_referrer": wiki_referrer,
                    "markdown_referrer": markdown_referrer,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
