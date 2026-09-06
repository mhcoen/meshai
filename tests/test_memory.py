"""Per-person memory: caps, age, population, forget, and how it reaches the model."""

import pytest

from bot.memory import PersonMemory, Round, render_rounds
from bot.prompt import HISTORY_BEGIN, MEMORY_BEGIN, MEMORY_END
from bot.service import Decision
from tests.conftest import FakeBackend


# ----------------------------------------------------------------------------- the store


def test_rounds_are_kept_per_person_oldest_first_and_capped(clock):
    m = PersonMemory(rounds=3, clock=clock)
    for i in range(5):
        m.record("alice", f"q{i}", f"a{i}")
    m.record("bob", "hi", "hello")
    assert [r.prompt for r in m.rounds_for("alice")] == ["q2", "q3", "q4"]
    assert [r.prompt for r in m.rounds_for("bob")] == ["hi"]
    assert m.rounds_for("carol") == []
    assert (m.people, m.total_rounds) == (2, 4)


def test_old_rounds_expire(clock):
    m = PersonMemory(rounds=10, max_age_s=100, clock=clock)
    m.record("alice", "old", "a")
    clock.advance(60)
    m.record("alice", "newer", "b")
    clock.advance(50)  # "old" is 110 s old, "newer" 50 s
    assert [r.prompt for r in m.rounds_for("alice")] == ["newer"]
    clock.advance(100)
    assert m.rounds_for("alice") == []
    assert m.sweep() == 1  # alice had nothing left and was removed
    assert m.people == 0


def test_population_cap_evicts_the_least_recently_seen(clock):
    m = PersonMemory(rounds=5, max_people=2, clock=clock)
    m.record("alice", "q", "a")
    m.record("bob", "q", "a")
    m.record("alice", "q2", "a2")  # alice seen again: bob is now the oldest
    m.record("carol", "q", "a")  # third person: bob goes
    assert m.rounds_for("bob") == []
    assert len(m.rounds_for("alice")) == 2 and len(m.rounds_for("carol")) == 1


def test_forget(clock):
    m = PersonMemory(clock=clock)
    m.record("alice", "q", "a")
    assert m.forget("alice") is True
    assert m.forget("alice") is False
    assert m.rounds_for("alice") == []


def test_bad_parameters():
    with pytest.raises(ValueError):
        PersonMemory(rounds=0)
    with pytest.raises(ValueError):
        PersonMemory(max_age_s=0)


def test_render_rounds_trims_from_the_oldest_end():
    rounds = [Round(0, "first question", "first reply"), Round(1, "second", "reply two")]
    assert render_rounds(rounds, 1000) == "asked: first question\nreplied: first reply\nasked: second\nreplied: reply two"
    assert render_rounds(rounds, 40) == "asked: second\nreplied: reply two"  # 31 chars
    assert render_rounds(rounds, 5) == ""
    assert render_rounds([], 100) == ""


# ----------------------------------------------------------------------------- through the service


async def test_answered_exchanges_are_remembered_and_shown_to_the_model_for_that_sender(harness):
    h = harness(backend=FakeBackend(replies=["Paris.", "It is in France.", "Four."]), global_burst=9, sender_burst=9)
    assert await h.say("Alice: capital of France") is Decision.ANSWERED
    assert h.backend.calls[0][1]["content"].count(MEMORY_BEGIN) == 0  # nothing remembered yet
    assert await h.say("Alice: and where is that") is Decision.ANSWERED
    user = h.backend.calls[1][1]["content"]
    block = user.split(MEMORY_BEGIN)[1].split(MEMORY_END)[0]
    assert "asked: capital of France\nreplied: Paris." in block
    assert user.index("and where is that") < user.index(MEMORY_BEGIN) < user.index(HISTORY_BEGIN)
    # Another sender sees no memory block for Alice's exchanges.
    assert await h.say("Bob: what is 2+2") is Decision.ANSWERED
    assert MEMORY_BEGIN not in h.backend.calls[2][1]["content"]
    assert (h.service.stats.people_remembered, h.service.stats.rounds_remembered) == (2, 3)


async def test_only_real_answers_are_remembered(harness):
    h = harness(backend=FakeBackend(replies=["word " * 60, "word " * 60, "word " * 60]), global_burst=9, sender_burst=9)
    assert await h.say("Alice: q") is Decision.ANSWERED_FALLBACK
    assert h.service.memory.rounds_for("Alice") == []
    h2 = harness(backend=FakeBackend(error=RuntimeError("x")), global_burst=9, sender_burst=9)
    assert await h2.say("Alice: q") is Decision.APOLOGY
    assert h2.service.memory.rounds_for("Alice") == []
    h3 = harness()
    await h3.say("Alice: one")
    assert await h3.say("Alice: two") is Decision.DROP_RATE_LIMITED
    assert [r.prompt for r in h3.service.memory.rounds_for("Alice")] == ["one"]


async def test_forget_command_wipes_and_confirms(harness):
    h = harness(global_burst=9, sender_burst=9)
    await h.say("Alice: remember this")
    assert h.service.memory.rounds_for("Alice")
    assert await h.say("Alice: /forget") is Decision.ANSWERED_FORGET
    assert h.sent[-1] == (1, "@[Alice] Forgotten.")
    assert h.service.memory.rounds_for("Alice") == []
    assert await h.say("Bob: /forget") is Decision.ANSWERED_FORGET
    assert h.sent[-1] == (1, "@[Bob] I had nothing on you.")
    assert h.backend.calls and len(h.backend.calls) == 1  # commands never reach the model


async def test_memory_block_is_part_of_the_injection_check(harness):
    class ComboGate:
        threshold = 0.45

        def check(self, text):
            from bot.guard import Verdict

            blocked = "alpha" in text and "beta" in text
            return Verdict(blocked=blocked, score=1.0 if blocked else 0.0, rules=("combo",) if blocked else (), text=text)

    h = harness(gate=ComboGate(), backend=FakeBackend(reply="ok"), global_burst=9, sender_burst=9)
    assert await h.say("Alice: alpha") is Decision.ANSWERED  # remembered: "asked: alpha / replied: ok"
    assert await h.say("Alice: beta") is Decision.DROP_INJECTION
    assert h.inbound_records()[-1]["point"] == "context"


async def test_memory_expires_with_the_configured_age(harness, clock):
    h = harness(person_memory_days=1, global_burst=9, sender_burst=9)
    await h.say("Alice: q")
    clock.advance(2 * 86400)
    assert h.service.memory.rounds_for("Alice") == []
