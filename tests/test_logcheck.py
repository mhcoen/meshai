"""The log checker: heard-but-undelivered detection with whitespace and own-reply handling."""

import json

from bot.cli import main
from bot.logcheck import check_log


def write_log(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def rx(ts, message, rssi=-80, snr=8.0):
    return {"ts": ts, "event": "rx", "ours": True, "message": message, "rssi": rssi, "snr": snr}


def inbound(ts, sender, prompt, decision="answered"):
    return {"ts": ts, "event": "inbound", "sender": sender, "prompt": prompt, "decision": decision}


def test_clean_window_reports_nothing_undelivered(tmp_path):
    log = tmp_path / "meshai.jsonl"
    write_log(log, [
        {"ts": "t0", "event": "startup", "bot_name": "MeshAI"},
        rx("t1", "Michael: Hello there, just testing "),  # trailing space, as the phone sends it
        inbound("t2", "Michael", "Hello there, just testing"),
        rx("t3", "MeshAI: @[Michael] Testing is for toddlers."),  # our reply back off a repeater
        rx("t4", "MeshAI: @[Michael] Testing is for toddlers.", rssi=-95, snr=2.0),  # second repeat
    ])
    result = check_log(log)
    assert result.undelivered == []
    assert (result.heard, result.heard_own, result.delivered) == (1, 2, 1)
    assert result.decisions == {"answered": 1}
    assert "Nothing heard by the radio went undelivered." in result.render()
    assert "1 heard from others on the channel, 2 own replies heard back, 1 delivered." in result.render()


def test_a_real_loss_is_listed_with_its_signal(tmp_path):
    log = tmp_path / "meshai.jsonl"
    write_log(log, [
        {"ts": "t0", "event": "startup", "bot_name": "MeshAI"},
        rx("2026-09-04T21:32:20+00:00", "Bob: anyone on tonight", rssi=-101, snr=-3.5),
        rx("2026-09-04T21:33:00+00:00", "Alice: hello", rssi=-70, snr=10.0),
        inbound("2026-09-04T21:33:01+00:00", "Alice", "hello", "dropped:rate-limited"),
    ])
    result = check_log(log)
    assert result.undelivered == [("2026-09-04T21:32:20+00:00", "Bob: anyone on tonight", -101, -3.5)]
    out = result.render()
    assert "Heard by the radio but never delivered to the bot:" in out
    assert "rssi=-101 snr=-3.5  Bob: anyone on tonight" in out
    assert "Decisions: dropped:rate-limited 1." in out
    assert "rssi min -101 median -70 max -70 dBm" in out


def test_unknown_bot_name_and_bad_lines_are_tolerated(tmp_path):
    log = tmp_path / "meshai.jsonl"
    log.write_text('not json\n' + json.dumps(rx("t1", "Zed: hi")) + "\n\n")
    result = check_log(log)
    assert result.bad_lines == 1
    assert result.heard == 1 and result.undelivered[0][1] == "Zed: hi"


def test_cli_check_prints_the_report_and_exits_zero(tmp_path, capsys):
    log = tmp_path / "meshai.jsonl"
    write_log(log, [{"ts": "t0", "event": "startup", "bot_name": "MeshAI"}, rx("t1", "A: q"), inbound("t2", "A", "q")])
    assert main(["--check", str(log)]) == 0
    assert "Nothing heard by the radio went undelivered." in capsys.readouterr().out


def test_cli_check_missing_file(tmp_path, capsys):
    assert main(["--check", str(tmp_path / "nope.jsonl")]) == 1
    assert "not found" in capsys.readouterr().err
