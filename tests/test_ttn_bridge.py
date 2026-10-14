import json
from pathlib import Path

import pytest

from ttn_bridge import (
    AlertState,
    Counters,
    append_to_spool,
    as_number,
    build_point,
    decode_payload,
    message_to_record,
    replay_spool,
    quality_flags,
)


def uplink(payload=None, received_at="2026-10-14T20:00:00Z"):
    return {
        "end_device_ids": {"device_id": "wind-01"},
        "uplink_message": {
            "received_at": received_at,
            "decoded_payload": payload or {"wind_speed": 5.2},
        },
    }


def test_as_number_rejects_non_finite_values():
    assert as_number("5.2") == 5.2
    assert as_number("NaN") is None
    assert as_number(float("inf")) is None


def test_decoder_matches_firmware_scaling():
    assert decode_payload(bytes.fromhex("020B0ABA034FF FFE".replace(" ", ""))) == {
        "wind_speed": 5.23,
        "wind_direction": 274.6,
        "gust": 8.47,
        "temperature": -0.2,
    }


def test_decoder_rejects_wrong_length():
    with pytest.raises(ValueError):
        decode_payload(b"\x00")


def test_message_requires_received_at_and_keeps_timestamp():
    record = message_to_record(uplink({"temperature": 21}))
    assert record["timestamp"] == "2026-10-14T20:00:00+00:00"
    assert build_point(uplink({"temperature": 21})) is not None


def test_invalid_payload_is_rejected():
    assert message_to_record(uplink({"wind_speed": "not-a-number"})) is None
    assert message_to_record(uplink({"wind_speed": 1}, received_at=None)) is None


def test_failed_records_can_be_spooled_and_replayed(tmp_path: Path):
    path = tmp_path / "failed-points.jsonl"
    record = message_to_record(uplink({"wind_speed": 1}))
    append_to_spool(record, path)
    counters = Counters()

    class WriteApi:
        def write(self, **kwargs):
            return None

    replay_spool(WriteApi(), bucket="bucket", org="org", path=path, counters=counters)
    assert not path.exists()


def test_quality_flags_detect_all_zero_without_rejecting_data():
    assert quality_flags({
        "wind_speed": 0,
        "wind_direction": 0,
        "gust": 0,
        "temperature": 0,
    }) == (True, False)
    assert quality_flags({"wind_speed": 31}) == (False, True)
    assert quality_flags({"wind_speed": 5, "gust": 4}) == (False, True)


def test_alert_state_logs_only_on_transitions(caplog, tmp_path: Path):
    counters = Counters()
    state = AlertState()
    counters.mark_received()
    counters.mark_stored()
    with caplog.at_level("ERROR", logger="ttn_bridge"):
        state.transition("test", True, "condition active")
        state.transition("test", True, "condition active")
    assert caplog.messages.count("ALERT: condition active") == 1
