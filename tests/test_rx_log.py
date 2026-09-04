"""RX log capture: what the radio heard, whether or not a message was delivered."""

from meshcore import EventType

HEARD_OURS = dict(payload_typename="GRP_TXT", route_typename="FLOOD", path_len=2, rssi=-85, snr=7.5,
                  chan_hash="98", chan_name="#ai", message="Alice: hello there", msg_hash=12345)
HEARD_OTHER = dict(payload_typename="GRP_TXT", route_typename="FLOOD", path_len=1, rssi=-90, snr=4.0,
                   chan_hash="11", chan_name="Public", message="Bob: hi", msg_hash=999)
HEARD_ADVERT = dict(payload_typename="ADVERT", route_typename="FLOOD", path_len=0, rssi=-60, snr=11.0)


async def test_channel_mode_logs_only_the_served_channel(harness):
    h = harness()
    await h.service.start()
    assert h.mc.decrypt_channel_logs is True
    assert any(t == EventType.RX_LOG_DATA for t, _cb, _f in h.mc.subscriptions)
    await h.mc.deliver_rx_log(**HEARD_OURS)
    await h.mc.deliver_rx_log(**HEARD_OTHER)
    await h.mc.deliver_rx_log(**HEARD_ADVERT)
    rx = [r for r in h.records if r["event"] == "rx"]
    assert len(rx) == 1
    assert rx[0]["ours"] is True and rx[0]["message"] == "Alice: hello there"
    assert (rx[0]["rssi"], rx[0]["snr"], rx[0]["path_len"], rx[0]["chan"]) == (-85, 7.5, 2, "#ai")
    assert h.service.stats.rx_heard == 1
    assert h.sent == [] and h.backend.calls == []  # hearing is not answering


async def test_all_mode_logs_everything_but_marks_ours(harness):
    h = harness(rx_log="all")
    await h.service.start()
    await h.mc.deliver_rx_log(**HEARD_OURS)
    await h.mc.deliver_rx_log(**HEARD_OTHER)
    await h.mc.deliver_rx_log(**HEARD_ADVERT)
    rx = [r for r in h.records if r["event"] == "rx"]
    assert [r["ours"] for r in rx] == [True, False, False]
    assert rx[1]["message"] is None  # other channels' text is never logged
    assert rx[2]["type"] == "ADVERT" and rx[2]["chan"] is None
    assert h.service.stats.rx_heard == 1


async def test_off_mode_subscribes_to_nothing(harness):
    h = harness(rx_log="off")
    await h.service.start()
    assert h.mc.decrypt_channel_logs is False
    assert not any(t == EventType.RX_LOG_DATA for t, _cb, _f in h.mc.subscriptions)


async def test_a_heard_message_and_its_delivery_are_both_in_the_log(harness):
    h = harness()
    await h.service.start()
    await h.mc.deliver_rx_log(**HEARD_OURS)
    await h.mc.deliver("Alice: hello there", channel_idx=1, path_len=2)
    events = [r["event"] for r in h.records if r["event"] in ("rx", "inbound")]
    assert events == ["rx", "inbound"]
