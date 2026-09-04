"""Configuration: a TOML file with flat keys grouped into sections, plus MESHAI_* env overrides.

Every key in the TOML file maps to one field of :class:`Config` regardless of which
section it sits in; sections exist only for readability. The environment variable
``MESHAI_<FIELD_NAME_UPPER>`` overrides any field. The OpenAI-compatible API key is
deliberately *not* a config field: it is read from ``MESHAI_OPENAI_API_KEY`` by the
backend so it can never end up in a log record or a dumped config.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

ENV_PREFIX = "MESHAI_"
API_KEY_ENV = "MESHAI_OPENAI_API_KEY"

BACKENDS = ("ollama", "openai")
THINK_MODES = ("off", "on", "omit")


class ConfigError(ValueError):
    """Raised for a malformed or invalid configuration."""


@dataclass(frozen=True)
class Config:
    # [radio]
    port: str = ""
    channel_idx: int = 1

    # [bot]
    bot_name: str = "MeshAI"
    trigger_prefix: str = ""
    reply_max_chars: int = 100
    prompt_max_chars: int = 140
    apology: str = "Sorry, I couldn't answer that one."
    persona: str = ""

    # [model]
    backend: str = "ollama"
    model: str = "qwen3:30b-a3b-instruct-2507-q4_K_M"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_think: str = "off"
    ollama_keep_alive: str = "30m"
    openai_base_url: str = "http://127.0.0.1:1234/v1"
    temperature: float = 0.3
    max_tokens: int = 80
    model_timeout_s: float = 30.0

    # [limits]
    global_rate_per_min: float = 2.0
    global_burst: int = 1
    sender_rate_per_min: float = 2.0
    sender_burst: int = 1

    # [history]
    history_size: int = 20
    transcript_max_chars: int = 1500

    # [adaptive]
    adaptive_enabled: bool = True
    utilization_poll_s: float = 10.0
    utilization_window_s: float = 60.0
    duty_low: float = 0.05
    duty_high: float = 0.15

    # [vordur]
    vordur_threshold: float = 0.45
    vordur_sanitize: bool = False

    # [logging]
    log_file: str = ""

    def validate(self) -> "Config":
        errors: list[str] = []
        if not self.port:
            errors.append("port is required (radio.port or MESHAI_PORT)")
        if not 0 <= self.channel_idx <= 255:
            errors.append("channel_idx must be in 0..255")
        if not self.bot_name.strip():
            errors.append("bot_name must not be empty")
        if self.backend not in BACKENDS:
            errors.append(f"backend must be one of {BACKENDS}")
        if self.ollama_think not in THINK_MODES:
            errors.append(f"ollama_think must be one of {THINK_MODES}")
        if not self.model:
            errors.append("model must not be empty")
        if self.reply_max_chars <= 0:
            errors.append("reply_max_chars must be positive")
        if self.prompt_max_chars <= 0:
            errors.append("prompt_max_chars must be positive")
        if self.model_timeout_s <= 0:
            errors.append("model_timeout_s must be positive")
        if self.max_tokens <= 0:
            errors.append("max_tokens must be positive")
        for name in ("global_rate_per_min", "sender_rate_per_min"):
            if getattr(self, name) <= 0:
                errors.append(f"{name} must be positive")
        for name in ("global_burst", "sender_burst", "history_size"):
            if getattr(self, name) < 1:
                errors.append(f"{name} must be at least 1")
        if self.transcript_max_chars < 0:
            errors.append("transcript_max_chars must not be negative")
        if not 0.0 <= self.vordur_threshold <= 1.0:
            errors.append("vordur_threshold must be between 0 and 1")
        if self.utilization_poll_s <= 0 or self.utilization_window_s < self.utilization_poll_s:
            errors.append("utilization_poll_s must be positive and no larger than utilization_window_s")
        if not 0.0 <= self.duty_low < self.duty_high <= 1.0:
            errors.append("need 0 <= duty_low < duty_high <= 1")
        if errors:
            raise ConfigError("; ".join(errors))
        return self


_FIELD_TYPES: dict[str, type] = {f.name: f.type for f in fields(Config)}  # type: ignore[misc]


def _coerce(name: str, value: Any) -> Any:
    """Coerce a TOML or env value to the declared field type."""
    target = _FIELD_TYPES[name]
    if isinstance(target, str):  # `from __future__ import annotations` leaves strings
        target = {"str": str, "int": int, "float": float, "bool": bool}[target]
    if target is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name}: expected a boolean, got {value!r}")
    if target is int:
        if isinstance(value, bool):
            raise ConfigError(f"{name}: expected an integer, got {value!r}")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected an integer, got {value!r}") from None
    if target is float:
        if isinstance(value, bool):
            raise ConfigError(f"{name}: expected a number, got {value!r}")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected a number, got {value!r}") from None
    return str(value)


def _flatten(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Merge top-level keys and one level of sections into a single flat dict."""
    flat: dict[str, Any] = {}
    for key, value in doc.items():
        items = value.items() if isinstance(value, Mapping) else [(key, value)]
        for name, val in items:
            if name not in _FIELD_TYPES:
                raise ConfigError(f"unknown config key: {name}")
            if name in flat:
                raise ConfigError(f"config key given twice: {name}")
            flat[name] = val
    return flat


def config_from_mapping(doc: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from an already-parsed TOML document plus env overrides."""
    values = {name: _coerce(name, val) for name, val in _flatten(doc).items()}
    env = os.environ if env is None else env
    for name in _FIELD_TYPES:
        raw = env.get(ENV_PREFIX + name.upper())
        if raw is not None:
            values[name] = _coerce(name, raw)
    return replace(Config(), **values).validate()


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    """Load ``path`` (TOML) and apply env overrides. Raises ConfigError on any problem."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            doc = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: {exc}") from None
    return config_from_mapping(doc, env)
