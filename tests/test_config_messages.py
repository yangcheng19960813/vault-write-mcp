from dataclasses import replace
import json

import httpx
import pytest

from vault_write_mcp.config import CommitConfig, ModelConfig, PushConfig, Settings, load_settings
from vault_write_mcp.messages import CommitMessages, MessageError


def model_config(**overrides):
    return replace(ModelConfig(enabled=True, api_url="https://model.invalid/v1/chat/completions", model="configured-model", max_diff_chars=256), **overrides)


def install_transport(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    options = []
    def factory(**kwargs):
        options.append(kwargs)
        return client
    monkeypatch.setattr("vault_write_mcp.messages.httpx.Client", factory)
    monkeypatch.setenv("VAULT_WRITE_MODEL_API_KEY", "test-only-key")
    return options


def test_example_configuration_preserves_defaults():
    assert load_settings("config.example.toml") == Settings()
    assert load_settings() == Settings()


@pytest.mark.parametrize("content", [
    "[unknown]\nenabled=true", "[model]\napi_key='not-accepted'", "[commit]\nmode='other'",
    "[commit]\ninterval_seconds=0", "[commit]\ninterval_seconds=nan", "[model]\nenabled='false'",
    "[model]\nmax_diff_chars=1", "[model]\nmax_output_tokens=128.5", "[model]\nfailure_policy='ignore'",
    "[model]\napi_key_env='bad-name'", "[model]\ntoken_limit_field='unknown'", "[push]\nremote='--all'",
    "[model]\nenabled=true\napi_url='file:///tmp/model'\nmodel='test'",
    "[auth]\nenabled='false'", "[auth]\nenabled=1",
])
def test_invalid_configuration_is_rejected(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_settings(path)


def test_one_bounded_model_request_and_no_tools(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "docs: 更新并发写入说明"}}]})
    options = install_transport(monkeypatch, handler)
    message = CommitMessages(model_config()).generate("diff\n" + "x" * 1000, "fallback")
    assert message == "docs: 更新并发写入说明"
    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-only-key"
    assert payload["model"] == "configured-model"
    assert len(payload["messages"][1]["content"]) <= 256
    assert payload["messages"][1]["content"].endswith("[truncated]\n")
    assert payload["max_completion_tokens"] == 256
    assert "tools" not in payload and "max_tokens" not in payload
    assert options == [{"timeout": 10, "follow_redirects": False, "trust_env": False}]


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="test-only-key"), httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": "not a subject\nsecond line"}}]}),
    httpx.Response(200, text="x" * 70000), httpx.Response(302, headers={"location": "https://other.invalid"}),
])
def test_failure_falls_back_without_retry_or_secret_logging(monkeypatch, caplog, response):
    requests = []
    def handler(request):
        requests.append(request)
        return response
    install_transport(monkeypatch, handler)
    assert CommitMessages(model_config()).generate("diff", "default subject") == "default subject"
    assert len(requests) == 1
    assert "test-only-key" not in caplog.text and "model.invalid" not in caplog.text


def test_timeout_can_defer_instead_of_fallback(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("private failure detail", request=request)
    install_transport(monkeypatch, handler)
    with pytest.raises(MessageError, match="generation failed"):
        CommitMessages(model_config(failure_policy="defer")).generate("diff", "default")


def test_disabled_model_never_constructs_http_client(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("HTTP should not be called")
    monkeypatch.setattr("vault_write_mcp.messages.httpx.Client", unexpected)
    assert CommitMessages(ModelConfig()).generate("diff", "default") == "default"


def test_enabled_model_requires_the_configured_secret(monkeypatch):
    monkeypatch.delenv("VAULT_WRITE_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="VAULT_WRITE_MODEL_API_KEY"):
        CommitMessages(model_config())


def test_legacy_token_limit_field_is_configurable(monkeypatch):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 64 and "max_completion_tokens" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": "docs: 更新笔记"}}]})
    install_transport(monkeypatch, handler)
    assert CommitMessages(model_config(token_limit_field="max_tokens", max_output_tokens=64)).generate("diff", "fallback") == "docs: 更新笔记"
