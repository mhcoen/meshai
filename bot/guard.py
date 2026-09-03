"""The vordur gate: prompt-injection detection only, stateless, fail closed.

Uses ``vordur.security.prompt_injection_detector.detect_prompt_injection`` directly.
It returns a score in [0, 1] and the matched rule names; vordur's own cutoff is 0.45,
which is also our default threshold. The optional ``sanitize`` pass runs vordur's
sanitizer (invisible-character stripping, confusable folding, encoded-payload
decoding) before detection and returns the cleaned text.

Any exception raised by vordur is reported as a block with ``error`` set. The caller
must treat that as "do not call the model, do not transmit".
"""

from __future__ import annotations

from dataclasses import dataclass

from vordur.security.prompt_injection_detector import detect_prompt_injection
from vordur.security.sanitizer import sanitize as vordur_sanitize
from vordur.security.types import ContentType


@dataclass(frozen=True)
class Verdict:
    blocked: bool
    score: float
    rules: tuple[str, ...]
    text: str  # the text that was scored (sanitized when that mode is on)
    error: str | None = None


class InjectionGate:
    def __init__(self, threshold: float = 0.45, sanitize: bool = False):
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        self.threshold = float(threshold)
        self.sanitize = bool(sanitize)

    def check(self, text: str) -> Verdict:
        """Score ``text``; never raises."""
        try:
            scored = text
            if self.sanitize:
                scored = vordur_sanitize(text, ContentType.PLAINTEXT).cleaned_text
            sig = detect_prompt_injection(scored, ContentType.PLAINTEXT)
            score = float(sig.score)
            return Verdict(
                blocked=score >= self.threshold,
                score=score,
                rules=tuple(sig.matched_rules),
                text=scored,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            return Verdict(
                blocked=True,
                score=1.0,
                rules=(),
                text=text,
                error=f"{type(exc).__name__}: {exc}",
            )
