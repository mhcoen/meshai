"""Only exact-name mentions may use Unicode; answers obey both wire budgets."""

import pytest

from bot.reply import compose_reply, first_sentence, reply_body_room, reply_prefix
from bot.service import Decision
from tests.conftest import FakeBackend


@pytest.mark.parametrize("sender", [
    "\U0001f31fAndy0", "Andr\u00e9", "Andre\u0301", "\u738b\u660e",
    "\U0001f469\u200d\U0001f4bbDev", "\u2764\ufe0fNode",
])
async def test_mentions_preserve_names_without_normalization(harness, sender):
    h = harness(backend=FakeBackend(reply="Caf\u00e9 is open."))
    assert await h.say(f"{sender}: hello") is Decision.ANSWERED
    assert h.sent == [(1, f"@[{sender}] Cafe is open.")]
    assert h.service.memory.rounds_for(sender)


@pytest.mark.parametrize("bot_name", ["MeshAI", "M\u00e9shAI"])
async def test_exact_utf8_wire_boundary_is_sent_whole(harness, bot_name):
    sender = "\U0001f31fAndy0"
    max_bytes = 160 - len(f"{bot_name}: ".encode("utf-8"))
    available = reply_body_room(sender, 150, max_bytes)
    answer = "x" * (available - 1) + "."
    h = harness(bot_name=bot_name, backend=FakeBackend(reply=answer))
    assert await h.say(f"{sender}: hello") is Decision.ANSWERED
    text = h.sent[0][1]
    assert text == reply_prefix(sender) + answer
    assert len(text) <= 150
    assert len(f"{bot_name}: {text}".encode("utf-8")) == 160


async def test_reply_that_fits_characters_but_not_bytes_is_shortened(harness):
    sender = "\U0001f31fAndy0"
    available = reply_body_room(sender, 150, 152)
    too_long = "x" * available + "."
    assert len(reply_prefix(sender) + too_long) <= 150
    h = harness(backend=FakeBackend(replies=[too_long, "Short answer."]))
    assert await h.say(f"{sender}: hello") is Decision.ANSWERED
    assert len(h.backend.calls) == 2
    assert f"hard limit is {available}" in h.backend.calls[1][-1]["content"]
    assert h.sent == [(1, f"@[{sender}] Short answer.")]


async def test_unicode_mention_fallback_is_not_truncated(harness):
    sender = "\U0001f31fAndy0"
    h = harness(backend=FakeBackend(reply="word " * 100))
    assert await h.say(f"{sender}: hello") is Decision.ANSWERED_FALLBACK
    assert h.sent == [(1, reply_prefix(sender) + h.cfg.too_long_reply)]
    assert len(f"{h.cfg.bot_name}: {h.sent[0][1]}".encode("utf-8")) <= 160


@pytest.mark.parametrize("question,decision", [
    ("hello", Decision.APOLOGY),
    ("/reset", Decision.ANSWERED_RESET), ("/forget", Decision.ANSWERED_FORGET),
])
async def test_apology_and_command_mentions_keep_unicode(harness, question, decision):
    sender = "\U0001f31fAndy0"
    h = harness(backend=FakeBackend(error=RuntimeError("offline")), global_burst=2, sender_burst=2)
    assert await h.say(f"{sender}: {question}") is decision
    text = h.sent[0][1]
    assert text.startswith(reply_prefix(sender))
    assert text[len(reply_prefix(sender)):].isascii()
    assert len(f"{h.cfg.bot_name}: {text}".encode("utf-8")) <= 160


@pytest.mark.parametrize("question", ["hello"])
async def test_name_leaving_no_byte_room_is_rejected_before_token(harness, question):
    sender = "\U0001f31f" * 40
    h = harness()
    assert await h.say(f"{sender}: {question}") is Decision.DROP_EMPTY
    assert h.backend.calls == [] and h.sent == []
    assert h.limiter.snapshot()["global_tokens"] == 1


@pytest.mark.parametrize("sender", ["A\nB", "A\x00B", "A\u2028B", "A\ud800B"])
def test_unsafe_names_are_rejected_not_rewritten(sender):
    assert compose_reply(sender, "Answer.", 150, max_bytes=152) is None


@pytest.mark.parametrize("reply,sender", [
    ("@[Alice] Caf\u00e9.", "Alice"),
    ("@[\U0001f31fAndy0] Four.", None),
    ("@[Other] Four.", "Alice"),
    ("@[Alice] " + "x" * 144, "Alice"),
    ("@[\U0001f31fAndy0] " + "x" * 140, "\U0001f31fAndy0"),
])
async def test_send_rechecks_body_and_both_budgets(harness, reply, sender):
    h = harness()
    async with h.service._request(sender or h.cfg.bot_name):
        assert h.service._admit(sender or h.cfg.bot_name).allowed
        assert not await h.service._send(reply, mention_sender=sender)
    assert h.sent == []
    assert h.limiter.snapshot()["global_tokens"] == 1


@pytest.mark.parametrize("text,expected", [
    ("Choose plan B. Then relax.", "Choose plan B."),
    ("Take vitamin C. Drink water.", "Take vitamin C."),
    ("Ask Dr. Smith. Then wait.", "Ask Dr. Smith."),
    ("Use e.g. a pencil. Then wait.", "Use e.g. a pencil."),
    ("The U.S. team won. Celebrate.", "The U.S. team won."),
])
def test_abbreviations_require_an_interior_dot_or_known_title(text, expected):
    assert first_sentence(text) == expected
