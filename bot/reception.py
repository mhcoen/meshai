"""Bounded reception facts from this delivered event, never global last-packet stats."""

from typing import Any


def _measurement(payload: dict[str, Any], key: str, low: float, high: float) -> str:
    # MeshCore exposes uppercase RSSI/SNR on CHANNEL_MSG_RECV, unlike RX logs.
    value = payload.get(key)
    if type(value) not in (int, float) or not low <= value <= high:
        return "unavailable"
    return f"{value:g}"


def reception_context(payload: dict[str, Any]) -> str:
    """Snapshot validated scalar fields before queueing; no per-sender cache.

    Firmware V3 supplies SNR; the library attaches RSSI from the latest matching
    heard copy, which may have followed a different route. Older frames get both
    RSSI and SNR from that log. The event does not explicitly identify its version,
    so do not infer provenance from field presence or matching path lengths.
    The path-length sentinel 255 does not mean 255 hops.
    """
    hops = payload.get("path_len")
    hop_text = str(hops) if type(hops) is int and 0 <= hops <= 63 else "unavailable"
    return (
        "Current question at this bot, copies may differ:\n"
        f"RSSI in dBm: {_measurement(payload, 'RSSI', -128, 127)} "
        "(latest library-matched heard copy); "
        f"SNR in dB: {_measurement(payload, 'SNR', -32, 31.75)} "
        "(delivered copy on V3, matched heard copy on older frames); "
        f"reported hop count: {hop_text} (delivered copy).\n"
        "Not a verified single reception, even if path lengths match; not every hop. "
        "Repeater identities and measurements of earlier messages are unavailable."
    )
