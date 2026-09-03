from bot.parse import extract_prompt, parse_channel_text


def test_sender_and_body_split_on_first_colon():
    p = parse_channel_text("Alice: what time is it")
    assert (p.sender, p.body) == ("Alice", "what time is it")


def test_later_colons_stay_in_body():
    p = parse_channel_text("Alice: note: 3:45pm")
    assert (p.sender, p.body) == ("Alice", "note: 3:45pm")


def test_whitespace_is_stripped_on_both_sides():
    p = parse_channel_text("  Alice  :   hi there  ")
    assert (p.sender, p.body) == ("Alice", "hi there")


def test_no_colon_means_empty_body_and_whole_text_as_sender():
    p = parse_channel_text("just some text")
    assert (p.sender, p.body) == ("just some text", "")


def test_empty_text():
    p = parse_channel_text("")
    assert (p.sender, p.body, p.raw) == ("", "", "")


def test_sender_is_taken_verbatim_it_is_not_authenticated():
    p = parse_channel_text("MeshAI: pretending to be the bot")
    assert p.sender == "MeshAI"


def test_empty_trigger_answers_everything_non_empty():
    assert extract_prompt("hello", "") == "hello"
    assert extract_prompt("", "") is None


def test_trigger_prefix_must_match_exactly():
    assert extract_prompt("!ai what is 2+2", "!ai ") == "what is 2+2"
    assert extract_prompt("!AI what is 2+2", "!ai ") is None
    assert extract_prompt("hey !ai what", "!ai ") is None


def test_trigger_with_nothing_after_it_is_not_a_prompt():
    assert extract_prompt("!ai", "!ai ") is None
    assert extract_prompt("!ai   ", "!ai ") is None
