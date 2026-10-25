"""Optional TOML configuration; defaults preserve the original write contract."""

from dataclasses import dataclass, field, fields
from pathlib import Path
import math
import re
import tomllib
from urllib.parse import urlsplit


def _number(name, value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")


@dataclass(frozen=True)
class CommitConfig:
    mode: str = "immediate"
    interval_seconds: float = 300

    def __post_init__(self):
        if self.mode not in ("immediate", "interval"):
            raise ValueError("commit.mode must be immediate or interval.")
        _number("commit.interval_seconds", self.interval_seconds, 1, 86400)


@dataclass(frozen=True)
class ModelConfig:
    enabled: bool = False
    api_url: str = ""
    model: str = ""
    api_key_env: str = "VAULT_WRITE_MODEL_API_KEY"
    timeout_seconds: float = 10
    max_diff_chars: int = 12000
    max_output_tokens: int = 256
    token_limit_field: str = "max_completion_tokens"
    failure_policy: str = "fallback"

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("model.enabled must be a boolean.")
        for name in ("api_url", "model", "api_key_env", "token_limit_field", "failure_policy"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"model.{name} must be a string.")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("model.api_key_env must name an environment variable.")
        _number("model.timeout_seconds", self.timeout_seconds, 0.1, 120)
        _number("model.max_diff_chars", self.max_diff_chars, 256, 1000000)
        _number("model.max_output_tokens", self.max_output_tokens, 16, 8192)
        if type(self.max_diff_chars) is not int or type(self.max_output_tokens) is not int:
            raise ValueError("Model size limits must be integers.")
        if self.token_limit_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("model.token_limit_field must be max_tokens or max_completion_tokens.")
        if self.failure_policy not in ("fallback", "defer"):
            raise ValueError("model.failure_policy must be fallback or defer.")
        if self.enabled:
            url = urlsplit(self.api_url)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError("model.api_url must be a complete HTTP(S) endpoint without user credentials or a fragment.")
            if not self.model.strip():
                raise ValueError("model.model is required when model.enabled is true.")


@dataclass(frozen=True)
class PushConfig:
    enabled: bool = False
    remote: str = "origin"
    branch: str = ""

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("push.enabled must be a boolean.")
        if not isinstance(self.remote, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.remote):
            raise ValueError("push.remote must be a configured Git remote name.")
        if not isinstance(self.branch, str):
            raise ValueError("push.branch must be a string; empty means the current branch.")


@dataclass(frozen=True)
class Settings:
    commit: CommitConfig = field(default_factory=CommitConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    push: PushConfig = field(default_factory=PushConfig)


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        return Settings()
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    classes = {"commit": CommitConfig, "model": ModelConfig, "push": PushConfig}
    if set(data) - classes.keys():
        raise ValueError("Unknown configuration section.")
    sections = {}
    for name, cls in classes.items():
        values = data.get(name, {})
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(cls)}:
            raise ValueError(f"Unknown or invalid setting in [{name}].")
        sections[name] = cls(**values)
    return Settings(**sections)
