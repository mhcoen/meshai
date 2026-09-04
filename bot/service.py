"""The bot itself: one handler for every channel message, with the full decision path.

Order of checks for an inbound message (first failure wins, every outcome is logged):
  1. loop guard   - sender is the bot, or the body starts with "@["
  2. trigger      - body must start with the configured prefix ("" = everything)
  3. length       - prompt over prompt_max_chars
  4. injection    - the prompt itself
  5. rate limits  - global and per-sender tokens, taken once, here
then: assemble context -> injection check on transcript+prompt -> model (hard timeout) ->
injection check on the reply -> shape -> cap -> send. A model timeout or error sends the
fixed apology on the tokens already taken. An injection block anywhere sends nothing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from meshcore import EventType

from bot.backends import Backend
from bot.config import Config
from bot.guard import InjectionGate
from bot.history import History, HistoryEntry, render_transcript
from bot.jsonlog import EventLog
from bot.parse import extract_prompt, parse_channel_text
from bot.prompt import build_messages
from bot.ratelimit import RateLimiter
from bot.reply import compose_reply, shape_reply


class Decision(str, Enum):
    ANSWERED = "answered"
    APOLOGY = "apology"
    DROP_LOOP_GUARD = "dropped:loop-guard"
    DROP_NO_TRIGGER = "dropped:no-trigger"
    DROP_TOO_LONG = "dropped:too-long"
    DROP_INJECTION = "dropped:injection-blocked"
    DROP_RATE_LIMITED = "dropped:rate-limited"
    DROP_EMPTY = "dropped:empty-reply"
    DROP_SEND_FAILED = "dropped:send-failed"
    IGNORED_OTHER_CHANNEL = "ignored:other-channel"


class ChannelError(RuntimeError):
    """The configured channel index is empty or could not be read."""


@dataclass
class Stats:
    connected: bool = False
    channel_name: str = ""
    channel_idx: int = -1
    received: int = 0
    replies_sent: int = 0
    apologies_sent: int = 0
    injection_blocks: int = 0
    rate_limited: int = 0
    send_errors: int = 0
    model_errors: int = 0
    last_latency_ms: float | None = None
    last_decision: str = ""
    started_at: float = field(default_factory=time.time)


class BotService:
    def __init__(
        self,
        cfg: Config,
        meshcore: Any,
        backend: Backend,
        gate: InjectionGate,
        limiter: RateLimiter,
        history: History,
        log: EventLog,
        clock: Callable[[], float] = time.monotonic,
        monitor: Any = None,
    ):
        self.cfg = cfg
        self.mc = meshcore
        self.backend = backend
        self.gate = gate
        self.limiter = limiter
        self.history = history
        self.log = log
        self.monitor = monitor  # optional UtilizationMonitor; started/stopped with the service
        self._clock = clock
        self.stats = Stats(channel_idx=cfg.channel_idx)
        self._subs: list[Any] = []
        self._stopped = False
        self._last_sent: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        result = await self.mc.commands.get_channel(self.cfg.channel_idx)
        if result is None or result.type == EventType.ERROR:
            raise ChannelError(
                f"get_channel({self.cfg.channel_idx}) failed: {getattr(result, 'payload', None)}"
            )
        name = (result.payload or {}).get("channel_name", "")
        if not name:
            raise ChannelError(
                f"channel {self.cfg.channel_idx} is empty on this radio; create it first"
            )
        self.stats.channel_name = name
        self.stats.connected = bool(getattr(self.mc, "is_connected", True))
        self.log.emit(
            "startup",
            channel_idx=self.cfg.channel_idx,
            channel_name=name,
            bot_name=self.cfg.bot_name,
            backend=self.backend.name,
            model=self.cfg.model,
            trigger_prefix=self.cfg.trigger_prefix,
        )
        self._subs.append(
            self.mc.subscribe(
                EventType.CHANNEL_MSG_RECV,
                self._on_channel_message,
                attribute_filters={"channel_idx": self.cfg.channel_idx},
            )
        )
        self._subs.append(self.mc.subscribe(EventType.CONNECTED, self._on_connected))
        self._subs.append(self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnected))
        await self.mc.start_auto_message_fetching()
        if self.monitor is not None:
            self.monitor.start()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.monitor is not None:
            await self.monitor.stop()
        for sub in self._subs:
            try:
                self.mc.unsubscribe(sub)
            except Exception:  # noqa: BLE001
                pass
        self._subs.clear()
        for step in (self.mc.stop_auto_message_fetching, self.mc.disconnect, self.backend.aclose):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001
                self.log.emit("shutdown_error", step=getattr(step, "__name__", str(step)), error=str(exc))
        self.stats.connected = False
        self.log.emit("shutdown", replies_sent=self.stats.replies_sent)

    async def _on_connected(self, event: Any) -> None:
        self.stats.connected = True
        self.log.emit("connected", payload=event.payload)

    async def _on_disconnected(self, event: Any) -> None:
        self.stats.connected = False
        self.log.emit("disconnected", payload=event.payload)

    async def _on_channel_message(self, event: Any) -> None:
        await self.handle_payload(event.payload or {})

    # ------------------------------------------------------------------ the handler

    def _record(self, parsed, path_len, decision: Decision, **extra: Any) -> Decision:
        self.stats.last_decision = decision.value
        self.log.emit(
            "inbound",
            sender=parsed.sender,
            prompt=parsed.body,
            path_len=path_len,
            decision=decision.value,
            **extra,
        )
        return decision

    async def handle_payload(self, payload: dict[str, Any]) -> Decision:
        cfg = self.cfg
        chan = payload.get("channel_idx")
        text = payload.get("text", "") or ""
        path_len = payload.get("path_len")
        parsed = parse_channel_text(text)

        if chan != cfg.channel_idx:
            return self._record(parsed, path_len, Decision.IGNORED_OTHER_CHANNEL, channel_idx=chan)

        self.stats.received += 1

        # 1a. Our own post coming back. Already in history from send time; never answer it.
        if parsed.sender == cfg.bot_name:
            return self._record(parsed, path_len, Decision.DROP_LOOP_GUARD, reason="own-name")

        # Every foreign line goes into history, with its own injection verdict attached.
        line_verdict = self.gate.check(f"{parsed.sender}: {parsed.body}")
        self.history.append(
            HistoryEntry(
                sender=parsed.sender,
                text=parsed.body,
                flagged=line_verdict.blocked,
                score=line_verdict.score,
                rules=line_verdict.rules,
            )
        )
        if line_verdict.blocked:
            self.stats.injection_blocks += 1
            self.log.emit(
                "injection_block",
                point="transcript-line",
                sender=parsed.sender,
                score=line_verdict.score,
                rules=list(line_verdict.rules),
                error=line_verdict.error,
            )

        # 1b. Replies from bots (ours or anyone's) are never prompts.
        if parsed.body.startswith("@["):
            return self._record(parsed, path_len, Decision.DROP_LOOP_GUARD, reason="reply-prefix")

        # 2. Trigger.
        prompt = extract_prompt(parsed.body, cfg.trigger_prefix)
        if prompt is None:
            return self._record(parsed, path_len, Decision.DROP_NO_TRIGGER)

        # 3. Length.
        if len(prompt) > cfg.prompt_max_chars:
            return self._record(
                parsed, path_len, Decision.DROP_TOO_LONG, prompt_len=len(prompt), cap=cfg.prompt_max_chars
            )

        # 4. Vordur on the prompt.
        verdict = self.gate.check(prompt)
        if verdict.blocked:
            self.stats.injection_blocks += 1
            return self._record(
                parsed,
                path_len,
                Decision.DROP_INJECTION,
                point="prompt",
                injection_score=verdict.score,
                injection_rules=list(verdict.rules),
                injection_error=verdict.error,
            )
        prompt = verdict.text  # sanitized form when that mode is on

        # 5. Rate limits, taken once for whatever we end up sending.
        limit = self.limiter.allow(parsed.sender)
        if not limit.allowed:
            self.stats.rate_limited += 1
            return self._record(parsed, path_len, Decision.DROP_RATE_LIMITED, reason=limit.reason)

        # Context. The triggering line is already the newest history entry; exclude it.
        transcript = self._transcript_excluding_latest()
        context_verdict = self.gate.check(f"{transcript}\n{prompt}" if transcript else prompt)
        if context_verdict.blocked:
            self.stats.injection_blocks += 1
            return self._record(
                parsed,
                path_len,
                Decision.DROP_INJECTION,
                point="context",
                injection_score=context_verdict.score,
                injection_rules=list(context_verdict.rules),
                injection_error=context_verdict.error,
            )

        prefix_len = len(f"@[{parsed.sender}] ")
        # Models overshoot a stated character budget by 10 to 20 percent, so state 80 percent of
        # the real room; the hard cap in compose_reply still enforces the true limit.
        budget = max(1, int((cfg.reply_max_chars - prefix_len) * 0.8))
        messages = build_messages(cfg.bot_name, budget, transcript, prompt, cfg.persona)

        # Model, under a hard timeout.
        started = self._clock()
        try:
            raw = await asyncio.wait_for(self.backend.complete(messages), timeout=cfg.model_timeout_s)
            latency_ms = round((self._clock() - started) * 1000.0, 1)
            self.stats.last_latency_ms = latency_ms
        except asyncio.TimeoutError:
            self.stats.model_errors += 1
            self.stats.last_latency_ms = round((self._clock() - started) * 1000.0, 1)
            return await self._send_apology(parsed, path_len, reason="timeout")
        except Exception as exc:  # noqa: BLE001
            self.stats.model_errors += 1
            return await self._send_apology(parsed, path_len, reason=f"{type(exc).__name__}: {exc}")

        # Outbound.
        shaped = shape_reply(raw)
        out_verdict = self.gate.check(shaped)
        if out_verdict.blocked:
            self.stats.injection_blocks += 1
            return self._record(
                parsed,
                path_len,
                Decision.DROP_INJECTION,
                point="reply",
                injection_score=out_verdict.score,
                injection_rules=list(out_verdict.rules),
                injection_error=out_verdict.error,
                latency_ms=latency_ms,
            )
        reply = compose_reply(parsed.sender, shaped, cfg.reply_max_chars)
        if reply is None:
            return self._record(parsed, path_len, Decision.DROP_EMPTY, latency_ms=latency_ms)

        if await self._send(reply):
            self.stats.replies_sent += 1
            return self._record(parsed, path_len, Decision.ANSWERED, reply=reply, latency_ms=latency_ms)
        return self._record(parsed, path_len, Decision.DROP_SEND_FAILED, reply=reply, latency_ms=latency_ms)

    # ------------------------------------------------------------------ helpers

    def _transcript_excluding_latest(self) -> str:
        entries = self.history.entries()[:-1]
        lines = [e.line() for e in entries if not e.flagged]
        return render_transcript(lines, self.cfg.transcript_max_chars)

    async def _send_apology(self, parsed, path_len, reason: str) -> Decision:
        reply = compose_reply(parsed.sender, self.cfg.apology, self.cfg.reply_max_chars)
        if reply is None:
            return self._record(parsed, path_len, Decision.DROP_EMPTY, model_error=reason)
        if await self._send(reply):
            self.stats.apologies_sent += 1
            return self._record(parsed, path_len, Decision.APOLOGY, reply=reply, model_error=reason)
        return self._record(parsed, path_len, Decision.DROP_SEND_FAILED, reply=reply, model_error=reason)

    async def _send(self, reply: str) -> bool:
        assert len(reply) <= self.cfg.reply_max_chars
        try:
            result = await self.mc.commands.send_chan_msg(self.cfg.channel_idx, reply)
        except Exception as exc:  # noqa: BLE001
            self.stats.send_errors += 1
            self.log.emit("send_error", error=f"{type(exc).__name__}: {exc}")
            return False
        if result is None or result.type == EventType.ERROR:
            self.stats.send_errors += 1
            self.log.emit("send_error", error=str(getattr(result, "payload", None)))
            return False
        self._last_sent = reply
        self.history.append(HistoryEntry(sender=self.cfg.bot_name, text=reply))
        return True
