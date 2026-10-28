from dataclasses import replace
import json
import multiprocessing
import os
import subprocess
import sys

import pytest

from vault_write_mcp.config import CommitConfig, ModelConfig, PushConfig, Settings
from vault_write_mcp.store import VaultError, VaultStore, init_vault


INTERVAL = Settings(commit=CommitConfig(mode="interval", interval_seconds=1))


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def store(tmp_path):
    init_vault(tmp_path)
    return VaultStore(tmp_path, INTERVAL)


def crash(root, kind, stage):
    class CrashingStore(VaultStore):
        def _checkpoint(self, actual_stage):
            if actual_stage == stage and json.loads(self.journal.read_text())["kind"] == kind:
                os._exit(77)
    store = CrashingStore(root, INTERVAL)
    store.write_note("note.md", "pending\n", "missing", "crash", "test")
    if kind == "batch_commit":
        store.flush()


def test_interval_writes_are_durable_then_commit_as_one_batch(store):
    arguments = ("note.md", "# Start\n", "missing", "create", "A")
    first = store.write_note(*arguments)
    assert first["pending"] and first["commit"] is None
    second = store.append_note("note.md", "B\n", first["version"], "append", "B")
    store.write_note("other.md", "other", "missing", "other", "B")
    assert git(store.root, "rev-list", "--count", "HEAD") == "1"
    assert VaultStore(store.root, INTERVAL).read_note("note.md")["content"] == "# Start\nB\n"
    assert store.write_note(*arguments)["replayed"]
    with pytest.raises(VaultError, match="Re-read"):
        store.append_note("note.md", "stale", first["version"], "stale", "A")
    result = store.flush()
    assert result["changed"] and result["files"] == ["note.md", "other.md"]
    assert git(store.root, "rev-list", "--count", "HEAD") == "2"
    replay = store.write_note(*arguments)
    assert replay["replayed"] and not replay["pending"] and replay["commit"] == result["commit"]
    assert store.read_note("note.md")["version"] == second["version"]
    assert "VaultWrite-Actors: A, B" in git(store.root, "show", "-s", "--format=%B", "HEAD")
    assert git(store.root, "status", "--porcelain") == ""
    assert not store.flush()["changed"]


def test_switching_to_immediate_requires_flushing_pending_writes(store):
    created = store.write_note("note.md", "pending", "missing", "create", "test")
    immediate = VaultStore(store.root)
    with pytest.raises(VaultError) as caught:
        immediate.append_note("note.md", "extra", created["version"], "extra", "test")
    assert caught.value.code == "PENDING_BATCH"
    store.flush()
    assert immediate.append_note("note.md", "extra", created["version"], "extra", "test")["commit"]


def test_pending_noop_receipts_are_resolved_at_flush(store):
    first = store.write_note("note.md", "pending", "missing", "create", "test")
    arguments = ("note.md", "pending", first["version"], "noop", "test")
    assert store.write_note(*arguments)["pending"]
    result = store.flush()
    replay = store.write_note(*arguments)
    assert replay["commit"] == result["commit"] and not replay["pending"]


def test_net_zero_batch_does_not_call_model_or_create_commit(store, monkeypatch):
    original = VaultStore(store.root).write_note("note.md", "original", "missing", "seed", "test")
    changed = store.write_note("note.md", "changed", original["version"], "change", "test")
    store.write_note("note.md", "original", changed["version"], "restore", "test")
    monkeypatch.setattr(store.messages, "generate", lambda *args: pytest.fail("Net-zero batch must not call model"))
    assert not store.flush()["changed"]
    assert git(store.root, "rev-list", "--count", "HEAD") == "2"
    assert not store._pending_entries()


@pytest.mark.parametrize("kind,stage", [("pending_write", stage) for stage in ("prepared", "file_written", "receipt_saved")] + [("batch_commit", stage) for stage in ("prepared", "ref_updated", "receipt_saved")])
def test_deferred_and_batch_transactions_recover_after_actual_exit(store, kind, stage):
    process = multiprocessing.get_context("spawn").Process(target=crash, args=(str(store.root), kind, stage))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 77
    restarted = VaultStore(store.root, INTERVAL)
    assert restarted.read_note("note.md")["content"] == "pending\n"
    restarted.flush()
    replay = restarted.write_note("note.md", "pending\n", "missing", "crash", "test")
    assert replay["replayed"] and not replay["pending"]
    assert git(store.root, "rev-list", "--count", "HEAD") == "2"
    assert git(store.root, "status", "--porcelain") == ""
    git(store.root, "fsck", "--no-dangling")


def test_external_edits_are_preserved_and_block_flush(store):
    first = store.write_note("note.md", "pending", "missing", "create", "test")
    (store.root / "note.md").write_text("external")
    with pytest.raises(VaultError) as caught:
        store.flush()
    assert caught.value.code == "DIRTY_TARGET"
    with pytest.raises(VaultError):
        store.write_note("note.md", "overwrite", store.read_note("note.md")["version"], "external", "test")
    assert (store.root / "note.md").read_text() == "external"
    assert git(store.root, "rev-list", "--count", "HEAD") == "1"


def test_git_failure_preserves_batch_for_retry(store, monkeypatch):
    store.write_note("note.md", "pending", "missing", "create", "test")
    original = store._git
    def fail(*args, **kwargs):
        if args[0] == "update-ref":
            raise VaultError("GIT_ERROR", "injected ref failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "_git", fail)
    with pytest.raises(VaultError) as caught:
        store.flush()
    assert caught.value.code == "RECOVERY_REQUIRED" and store.journal.exists()
    monkeypatch.setattr(store, "_git", original)
    restarted = VaultStore(store.root, INTERVAL)
    assert not restarted._pending_entries()
    assert restarted.read_note("note.md")["content"] == "pending"
    assert git(store.root, "status", "--porcelain") == ""


def test_cli_can_flush_without_mcp_token(store):
    store.write_note("note.md", "pending", "missing", "create", "test")
    config = store.root / ".git/test.toml"
    config.write_text('[commit]\nmode="interval"\n')
    result = subprocess.run([sys.executable, "-m", "vault_write_mcp.server", "flush", "--vault", str(store.root), "--config", str(config)], capture_output=True, text=True, env={key: value for key, value in os.environ.items() if key != "VAULT_WRITE_TOKEN"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["commit"]["changed"]


def test_push_failure_retries_without_recommitting_or_calling_model(store, tmp_path):
    bare = tmp_path.parent / (tmp_path.name + "-remote.git")
    git(store.root, "remote", "add", "origin", str(bare))
    configured = VaultStore(store.root, replace(INTERVAL, push=PushConfig(enabled=True, branch="published")))
    configured.write_note("note.md", "pending", "missing", "create", "test")
    commit = configured.flush()["commit"]
    with pytest.raises(VaultError):
        configured.push()
    assert not (configured.state / "push.json").exists()
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    assert configured.push()["pushed"]
    assert git(bare, "rev-parse", "refs/heads/published") == commit
    assert not VaultStore(store.root, configured.settings).push()["pushed"]
    assert git(store.root, "rev-list", "--count", "HEAD") == "2"


def race_append(root, barrier, queue, actor):
    store = VaultStore(root, INTERVAL)
    current = store.read_note("note.md")
    barrier.wait(timeout=20)
    try:
        store.append_note("note.md", actor + "\n", current["version"], actor, actor)
        queue.put((actor, "success"))
    except VaultError as exc:
        queue.put((actor, exc.code))


def test_interval_cross_process_conflict_then_retry_and_batch(store):
    store.write_note("note.md", "# Start\n", "missing", "seed", "test")
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=race_append, args=(str(store.root), barrier, queue, actor)) for actor in ("A", "B")]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(status for _, status in results) == ["VERSION_CONFLICT", "success"]
    actor = next(actor for actor, status in results if status == "VERSION_CONFLICT")
    current = store.read_note("note.md")
    store.append_note("note.md", actor + "\n", current["version"], actor + "-retry", actor)
    assert store.read_note("note.md")["content"].count("A\n") == store.read_note("note.md")["content"].count("B\n") == 1
    store.flush()
    assert git(store.root, "rev-list", "--count", "HEAD") == "2"


def test_model_sees_only_pending_diff_once_per_batch_and_not_on_push_retry(store, monkeypatch):
    VaultStore(store.root).write_note("private.md", "private baseline", "missing", "private", "test")
    (store.root / "private.md").write_text("unrelated private edits")
    monkeypatch.setenv("VAULT_WRITE_MODEL_API_KEY", "test-only-key")
    configured = VaultStore(store.root, replace(INTERVAL, model=ModelConfig(enabled=True, api_url="https://model.invalid/v1/chat/completions", model="test")))
    configured.write_note("a.md", "new A", "missing", "A", "A")
    configured.write_note("b.md", "new B", "missing", "B", "B")
    diffs = []
    def generate(diff, fallback):
        diffs.append(diff)
        return "docs: 更新两篇知识笔记"
    monkeypatch.setattr(configured.messages, "generate", generate)
    configured.flush()
    configured.flush()
    configured.push()
    assert len(diffs) == 1
    assert "a.md" in diffs[0] and "b.md" in diffs[0]
    assert "private" not in diffs[0]
    assert git(store.root, "log", "-1", "--format=%s") == "docs: 更新两篇知识笔记"
    assert (store.root / "private.md").read_text() == "unrelated private edits"


def test_model_defer_keeps_batch_uncommitted_for_retry(store, monkeypatch):
    from vault_write_mcp.messages import MessageError
    store.write_note("note.md", "pending", "missing", "create", "test")
    def fail(*args):
        raise MessageError("generation failed")
    monkeypatch.setattr(store.messages, "generate", fail)
    with pytest.raises(VaultError) as caught:
        store.flush()
    assert caught.value.code == "MODEL_ERROR"
    assert git(store.root, "rev-list", "--count", "HEAD") == "1"
    assert store._pending_entries() and not store.journal.exists()
    monkeypatch.setattr(store.messages, "generate", lambda diff, fallback: fallback)
    assert store.flush()["changed"]


def test_push_uses_operator_global_git_configuration(store, tmp_path, monkeypatch):
    bare = tmp_path.parent / (tmp_path.name + "-global-remote.git")
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    global_config = tmp_path / ".git/operator.gitconfig"
    global_config.write_text('[url "' + str(bare) + '"]\n    insteadOf = vault-test-alias\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    git(store.root, "remote", "add", "origin", "vault-test-alias")
    configured = VaultStore(store.root, replace(INTERVAL, push=PushConfig(enabled=True)))
    configured.write_note("note.md", "pending", "missing", "create", "test")
    commit = configured.flush()["commit"]
    assert configured.push()["pushed"]
    assert git(bare, "rev-parse", "refs/heads/main") == commit


def test_push_rejects_remote_divergence_without_force(store, tmp_path):
    bare = tmp_path.parent / (tmp_path.name + "-divergent.git")
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    git(store.root, "remote", "add", "origin", str(bare))
    configured = VaultStore(store.root, replace(INTERVAL, push=PushConfig(enabled=True)))
    configured.write_note("note.md", "first", "missing", "first", "test")
    initial = configured.flush()["commit"]
    configured.push()
    tree = git(bare, "rev-parse", "refs/heads/main^{tree}")
    advanced = subprocess.check_output(["git", "-C", str(bare), "-c", "user.name=test", "-c", "user.email=test@localhost", "commit-tree", tree, "-p", initial], input="remote change\n", text=True).strip()
    git(bare, "update-ref", "refs/heads/main", advanced, initial)
    current = configured.read_note("note.md")
    configured.write_note("note.md", "local change", current["version"], "local", "test")
    configured.flush()
    with pytest.raises(VaultError):
        configured.push()
    assert git(bare, "rev-parse", "refs/heads/main") == advanced
    assert json.loads((configured.state / "push.json").read_text())["head"] == initial


def remote(store, path, name="origin"):
    subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)
    git(store.root, "remote", "add", name, str(path))
    return VaultStore(store.root, replace(INTERVAL, push=PushConfig(enabled=True, remote=name)))


def test_push_checkpoint_changes_when_same_named_remote_changes(store, tmp_path):
    first, second = tmp_path / "first.git", tmp_path / "second.git"
    configured = remote(store, first)
    subprocess.run(["git", "init", "--bare", str(second)], check=True, capture_output=True)
    assert configured.push()["pushed"]
    before = json.loads((store.state / "push.json").read_text())
    git(store.root, "remote", "set-url", "origin", str(second))
    assert configured.push()["pushed"]
    assert git(second, "rev-parse", "refs/heads/main") == configured._head()
    assert json.loads((store.state / "push.json").read_text())["destination_digest"] != before["destination_digest"]
    assert not configured.push()["pushed"]


def test_push_identity_uses_all_pushurls_not_fetch_url(store, tmp_path):
    configured = remote(store, tmp_path / "fetch.git")
    first, second = tmp_path / "push-one.git", tmp_path / "push-two.git"
    for path in (first, second):
        subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)
    git(store.root, "config", "--add", "remote.origin.pushurl", str(first))
    configured.push()
    git(store.root, "remote", "set-url", "origin", "unused-fetch-url")
    assert not configured.push()["pushed"]
    git(store.root, "config", "--add", "remote.origin.pushurl", str(second))
    assert configured.push()["pushed"]
    assert git(second, "rev-parse", "refs/heads/main") == configured._head()
    checkpoint = (store.state / "push.json").read_text()
    assert str(first) not in checkpoint and str(second) not in checkpoint
    git(store.root, "config", "--unset-all", "remote.origin.pushurl")
    for path in (second, first):
        git(store.root, "config", "--add", "remote.origin.pushurl", str(path))
    assert not configured.push()["pushed"]


def test_push_expanded_operator_alias_change_invalidates_checkpoint(store, tmp_path, monkeypatch):
    configured = remote(store, tmp_path / "first.git")
    second = tmp_path / "second.git"
    subprocess.run(["git", "init", "--bare", str(second)], check=True, capture_output=True)
    config = store.root / ".git/aliases"
    git(store.root, "remote", "set-url", "origin", "test-alias")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    for destination in (tmp_path / "first.git", second):
        config.write_text(f'[url "{destination}"]\n    pushInsteadOf = test-alias\n')
        assert configured.push()["pushed"]
    assert git(second, "rev-parse", "refs/heads/main") == configured._head()


def test_push_old_or_malformed_checkpoint_and_branch_changes_recheck(store, tmp_path):
    configured = remote(store, tmp_path / "remote.git")
    for saved in ('{"head":"legacy"}', 'not JSON'):
        (store.state / "push.json").write_text(saved)
        assert configured.push()["pushed"]
    changed = VaultStore(store.root, replace(configured.settings, push=PushConfig(enabled=True, branch="other")))
    assert changed.push()["pushed"]
    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/other") == configured._head()


def test_push_failure_never_exposes_remote_credentials_or_replaces_checkpoint(store, tmp_path, monkeypatch, caplog):
    configured = remote(store, tmp_path / "remote.git")
    configured.push()
    previous = (store.state / "push.json").read_bytes()
    original = configured._git
    secret_url = "https://private-user:private-token@private.invalid/vault.git"
    def fail(*args, **kwargs):
        if args[:2] == ("remote", "get-url"):
            return (secret_url + "\n").encode()
        if args[0] == "push":
            raise VaultError("GIT_ERROR", secret_url)
        return original(*args, **kwargs)
    monkeypatch.setattr(configured, "_git", fail)
    with pytest.raises(VaultError) as caught:
        configured.push()
    assert "private" not in json.dumps(caught.value.as_dict()) + caplog.text
    assert (store.state / "push.json").read_bytes() == previous
    monkeypatch.setattr(configured, "_git", original)
    assert not configured.push()["pushed"]


def test_push_rejects_ambiguous_control_delimited_destinations_without_leak(store, tmp_path):
    configured = remote(store, tmp_path / "remote.git")
    configured.push()
    previous = (store.state / "push.json").read_bytes()
    git(store.root, "remote", "set-url", "origin", "private-secret\nother-destination")
    with pytest.raises(VaultError) as caught:
        configured.push()
    assert "private-secret" not in str(caught.value)
    assert (store.state / "push.json").read_bytes() == previous
