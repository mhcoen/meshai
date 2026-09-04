from bot.reply import collapse_whitespace, compose_reply, first_sentence, shape_reply, strip_think


def test_collapse_whitespace_removes_newlines_and_runs():
    assert collapse_whitespace("  a \n\n b\t\tc  ") == "a b c"


def test_strip_think_removes_reasoning_blocks():
    assert strip_think("<think>\nplan\n</think>The answer.") == "The answer."


def test_first_sentence_keeps_only_the_first():
    assert first_sentence("It is sunny. Bring a hat!") == "It is sunny."
    assert first_sentence("Sure! And more. And more.") == "Sure!"


def test_a_leading_question_keeps_its_answer_but_nothing_after():
    assert first_sentence("Really? Yes.") == "Really? Yes."
    assert first_sentence("Why did the signal drop? Too many dead zones. Ask again later.") == (
        "Why did the signal drop? Too many dead zones."
    )
    assert first_sentence("Why did it drop? no terminator here") == "Why did it drop? no terminator here"
    assert first_sentence("Just a question?") == "Just a question?"


def test_output_is_plain_ascii_with_ordinary_punctuation():
    from bot.reply import plain_ascii

    assert plain_ascii("Paris \u2014 city of lights\u2026 \u201creally\u201d, \u2018yes\u2019 \u2013 ok") == (
        "Paris, city of lights. \"really\", 'yes', ok"
    )
    assert plain_ascii("caf\u00e9 au lait \U0001F600 done") == "cafe au lait done"
    assert plain_ascii("first; second") == "first, second"
    assert plain_ascii("well-known - yes") == "well-known, yes"  # spaced hyphen is a dash, inner hyphen stays
    assert shape_reply("Paris\u2014the answer.") == "Paris, the answer."
    assert shape_reply("Dead zones \u2014 obviously.") == "Dead zones, obviously."


def test_shaped_output_never_contains_non_ascii():
    out = shape_reply("\u201cSure\u201d \u2014 it\u2019s 3\u00b0C \U0001F976 tonight\u2026 maybe.")
    assert out == "\"Sure\", it's 3C tonight. maybe." or all(ord(c) < 128 for c in out)
    assert all(ord(c) < 128 for c in out)


def test_first_sentence_does_not_split_on_decimals_or_abbreviated_numbers():
    assert first_sentence("It is 3.5 km away. Walk.") == "It is 3.5 km away."


def test_first_sentence_without_terminator_returns_all():
    assert first_sentence("no punctuation here") == "no punctuation here"


def test_shape_reply_end_to_end():
    raw = '<think>hmm</think>\n"The capital is Paris.  It is\nlovely."'
    assert shape_reply(raw) == "The capital is Paris."


def test_compose_reply_prefixes_and_fits_cap():
    out = compose_reply("Alice", "Four.", 120)
    assert out == "@[Alice] Four."


def test_compose_reply_never_exceeds_cap_and_never_cuts_prefix():
    body = "word " * 100
    out = compose_reply("Alice", body, 120)
    assert out is not None
    assert len(out) <= 120
    assert out.startswith("@[Alice] ")
    assert not out.endswith(" ")


def test_compose_reply_cuts_at_word_boundary_when_reasonable():
    out = compose_reply("Al", "alpha beta gamma delta", 16)  # prefix "@[Al] " is 6 chars, 10 left
    assert out == "@[Al] alpha beta"[:16]
    assert len(out) <= 16


def test_compose_reply_hard_cuts_when_no_space_in_second_half():
    out = compose_reply("Al", "abcdefghijklmnopqrstuvwxyz", 16)
    assert out == "@[Al] abcdefghij"


def test_compose_reply_returns_none_when_sender_eats_the_budget():
    assert compose_reply("x" * 118, "hi", 120) is None
    assert compose_reply("x" * 200, "hi", 120) is None


def test_compose_reply_returns_none_for_empty_text():
    assert compose_reply("Alice", "   ", 120) is None
