"""The prompt injection gate: stateless, fail closed.

Wraps :func:`bot.injection.detect_prompt_injection`. The detector returns a score
in [0, 1] and the matched rule names; the gate compares the score with a
configurable threshold. Any exception is reported as a block with ``error`` set,
and the caller must treat that as "do not call the model, do not transmit".
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.injection import DEFAULT_THRESHOLD, detect_prompt_injection


@dataclass(frozen=True)
class Verdict:
    blocked: bool
    score: float
    rules: tuple[str, ...]
    text: str
    error: str | None = None


class InjectionGate:
    def __init__(self, threshold: float = DEFAULT_THRESHOLD):
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        self.threshold = float(threshold)

    def check(self, text: str) -> Verdict:
        """Score ``text``; never raises."""
        try:
            sig = detect_prompt_injection(text, self.threshold)
            return Verdict(blocked=sig.is_attack, score=float(sig.score), rules=tuple(sig.matched_rules), text=text)
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            return Verdict(blocked=True, score=1.0, rules=(), text=text, error=f"{type(exc).__name__}: {exc}")
