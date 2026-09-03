from bot.reply import collapse_whitespace, compose_reply, first_sentence, shape_reply, strip_think


def test_collapse_whitespace_removes_newlines_and_runs():
    assert collapse_whitespace("  a \n\n b\t\tc  ") == "a b c"


def test_strip_think_removes_reasoning_blocks():
    assert strip_think("<think>\nplan\n</think>The answer.") == "The answer."


def test_first_sentence_keeps_only_the_first():
    assert first_sentence("It is sunny. Bring a hat!") == "It is sunny."
    assert first_sentence("Really? Yes.") == "Really?"


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
