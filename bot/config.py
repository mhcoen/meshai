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
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from bot.fortune import parse_hhmm
from bot.personas import BUILTIN_PERSONAS, HELP_COMMAND, NAME_RE, RESET_COMMAND, build_help

ENV_PREFIX = "MESHAI_"
API_KEY_ENV = "MESHAI_OPENAI_API_KEY"

# MeshCore packs "<node name>: <text>" into at most this many bytes and silently truncates the rest.
WIRE_TEXT_MAX = 160

BACKENDS = ("ollama", "openai")
RX_LOG_MODES = ("off", "channel", "all")
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
    reply_max_chars: int = 150
    prompt_max_chars: int = 160
    reply_delay_s: float = 8.0
    shorten_retries: int = 2
    too_long_reply: str = "That answer will not fit in one message, ask me something narrower."
    apology: str = "Sorry, I couldn't answer that one."

    # [personas] table: name -> persona text (built-ins when absent), plus the keys below
    personas: dict[str, str] = field(default_factory=lambda: dict(BUILTIN_PERSONAS))
    default_persona: str = "funny"
    persona_timeout_min: float = 120.0
    persona_reset_message: str = "Back to the default personality."
    command_prefix: str = "/"

    # [model]
    backend: str = "ollama"
    model: str = "qwen3:30b-a3b-instruct-2507-q4_K_M"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_think: str = "off"
    ollama_keep_alive: str = "30m"
    openai_base_url: str = "http://127.0.0.1:1234/v1"
    temperature: float = 0.6
    max_tokens: int = 80
    model_timeout_s: float = 30.0

    # [fortune]
    fortune_enabled: bool = True
    fortune_time: str = "06:00"
    fortune_jitter_min: float = 12.0
    fortune_cutoff_min: float = 30.0
    fortune_prefix: str = "Fortune: "
    fortune_prompt: str = (
        "Write today's fortune for everyone on the channel: one silly, funny sentence in the style of a "
        "fortune cookie, somehow involving {subject}. Do not mention or address anyone. Today is {date}."
    )
    fortune_fallback: str = "The mesh is quiet this morning, and so is your fortune."

    # [limits]
    global_rate_per_min: float = 4.0
    global_burst: int = 1
    sender_rate_per_min: float = 4.0
    sender_burst: int = 1

    # [history]
    history_size: int = 20
    transcript_max_chars: int = 1500

    # [adaptive]
    adaptive_enabled: bool = True
    utilization_poll_s: float = 10.0
    utilization_window_s: float = 120.0
    duty_low: float = 0.05
    duty_high: float = 0.15
    tx_duty_budget: float = 0.02

    # [injection]
    injection_threshold: float = 0.45

    # [logging]
    log_file: str = ""
    rx_log: str = "channel"  # "off" | "channel" (packets on the served channel) | "all" (every packet heard)

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
        if self.rx_log not in RX_LOG_MODES:
            errors.append(f"rx_log must be one of {RX_LOG_MODES}")
        if self.ollama_think not in THINK_MODES:
            errors.append(f"ollama_think must be one of {THINK_MODES}")
        if not self.model:
            errors.append("model must not be empty")
        if self.reply_max_chars <= 0:
            errors.append("reply_max_chars must be positive")
        wire_room = WIRE_TEXT_MAX - len(self.bot_name.encode("utf-8")) - 2
        if self.reply_max_chars > wire_room:
            errors.append(
                f"reply_max_chars must be at most {wire_room} for bot_name {self.bot_name!r}: the radio packs "
                f"'{self.bot_name}: ' plus the reply into {WIRE_TEXT_MAX} bytes and cuts the rest"
            )
        if self.prompt_max_chars <= 0:
            errors.append("prompt_max_chars must be positive")
        if self.shorten_retries < 0:
            errors.append("shorten_retries must not be negative")
        if not self.too_long_reply.strip():
            errors.append("too_long_reply must not be empty")
        if self.reply_delay_s < 0:
            errors.append("reply_delay_s must not be negative")
        if self.model_timeout_s <= 0:
            errors.append("model_timeout_s must be positive")
        try:
            parse_hhmm(self.fortune_time)
        except ValueError as exc:
            errors.append(str(exc))
        if self.fortune_jitter_min < 0 or self.fortune_cutoff_min < 0:
            errors.append("fortune_jitter_min and fortune_cutoff_min must not be negative")
        if not self.fortune_prompt.strip() or "{subject}" not in self.fortune_prompt:
            errors.append("fortune_prompt must contain {subject}")
        else:
            try:
                self.fortune_prompt.format(subject="x", date="y")
            except (KeyError, IndexError, ValueError) as exc:
                errors.append(f"fortune_prompt has a bad placeholder ({exc}); only {{subject}} and {{date}} are allowed")
        if self.reply_max_chars > 0 and len(self.fortune_prefix) + len(self.fortune_fallback) > self.reply_max_chars:
            errors.append("fortune_prefix plus fortune_fallback must fit in reply_max_chars")
        if not self.personas:
            errors.append("personas must not be empty")
        for name, text in self.personas.items():
            if not NAME_RE.match(name):
                errors.append(f"persona name {name!r} must be lowercase letters, digits, underscores, at most 16 chars")
            if name in (HELP_COMMAND, RESET_COMMAND):
                errors.append(f"persona name {name!r} collides with a command")
            if not isinstance(text, str) or not text.strip():
                errors.append(f"persona {name!r} must have non-empty text")
        if self.default_persona not in self.personas:
            errors.append(f"default_persona {self.default_persona!r} is not in personas")
        if self.persona_timeout_min <= 0:
            errors.append("persona_timeout_min must be positive")
        if not self.command_prefix or " " in self.command_prefix:
            errors.append("command_prefix must be non-empty and contain no spaces")
        if not self.persona_reset_message.strip():
            errors.append("persona_reset_message must not be empty")
        room = self.reply_max_chars - len("@[") - 20 - len("] ")  # a 20 char sender name
        if self.reply_max_chars > 0 and len(self.help_message) > room:
            errors.append(f"the help line is {len(self.help_message)} chars; it must fit in {room} (fewer or shorter persona names)")
        if self.reply_max_chars > 0 and len(self.persona_reset_message) > self.reply_max_chars:
            errors.append("persona_reset_message must fit in reply_max_chars")
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
        if not 0.0 <= self.injection_threshold <= 1.0:
            errors.append("injection_threshold must be between 0 and 1")
        if self.utilization_poll_s <= 0 or self.utilization_window_s < self.utilization_poll_s:
            errors.append("utilization_poll_s must be positive and no larger than utilization_window_s")
        if not 0.0 < self.tx_duty_budget <= 0.5:
            errors.append("tx_duty_budget must be between 0 and 0.5")
        if not 0.0 <= self.duty_low < self.duty_high <= 1.0:
            errors.append("need 0 <= duty_low < duty_high <= 1")
        if errors:
            raise ConfigError("; ".join(errors))
        return self

    @property
    def help_message(self) -> str:
        return build_help(list(self.personas), self.persona_timeout_min, self.command_prefix)

    @property
    def default_persona_text(self) -> str:
        return self.personas[self.default_persona]


_FIELD_TYPES: dict[str, type] = {f.name: f.type for f in fields(Config)}  # type: ignore[misc]


def _coerce(name: str, value: Any) -> Any:
    """Coerce a TOML or env value to the declared field type."""
    if name == "personas":
        if not isinstance(value, Mapping):
            raise ConfigError("personas must be a table of name = \"text\"")
        return {str(k): str(v) for k, v in value.items()}
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
        if key == "personas":
            flat[key] = value  # a table of presets, not a section of settings
            continue
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
        if name == "personas":
            continue  # a table; no environment form
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
