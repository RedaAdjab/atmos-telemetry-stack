import json
import logging
import math
import os
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()

LOGGER = logging.getLogger("ttn_bridge")
MEASUREMENT = "wind_telemetry"
HEALTH_MEASUREMENT = "bridge_health"
PAYLOAD_FIELDS = {
    "wind_speed": "wind_speed",
    "wind_direction": "wind_direction",
    "gust": "gust",
    "temperature": "temperature",
}
SPOOL_PATH = Path(os.getenv("SPOOL_PATH", "spool/failed-points.jsonl"))
MAX_WRITE_ATTEMPTS = 4
INITIAL_RETRY_DELAY = 1.0
EXPECTED_INTERVAL_SECONDS = float(os.getenv("EXPECTED_INTERVAL_SECONDS", "120"))
STALE_MISSED_CYCLES = int(os.getenv("STALE_MISSED_CYCLES", "3"))
SPOOL_ALERT_SECONDS = float(os.getenv("SPOOL_ALERT_SECONDS", "300"))
QUALITY_LIMITS = {
    "wind_speed": (0.0, 30.0),
    "gust": (0.0, 30.0),
    "wind_direction": (0.0, 360.0),
    "temperature": (-50.0, 60.0),
}


def required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("your-") or value.startswith("change-this"):
        raise RuntimeError(f"Set {name} in .env before starting the bridge")
    return value


def as_number(value: object) -> float | None:
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def decode_payload(payload: bytes | bytearray) -> dict[str, float]:
    """Decode the firmware's eight-byte big-endian application payload."""
    if len(payload) != 8:
        raise ValueError("firmware payload must contain exactly 8 bytes")
    return {
        "wind_speed": int.from_bytes(payload[0:2], "big") / 100,
        "wind_direction": int.from_bytes(payload[2:4], "big") / 10,
        "gust": int.from_bytes(payload[4:6], "big") / 100,
        "temperature": int.from_bytes(payload[6:8], "big", signed=True) / 10,
    }


def parse_received_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("uplink is missing received_at")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def message_to_record(message: dict[str, Any]) -> dict[str, Any] | None:
    device_id = message.get("end_device_ids", {}).get("device_id")
    uplink = message.get("uplink_message", {})
    payload = uplink.get("decoded_payload")
    if not device_id or not isinstance(payload, dict):
        LOGGER.warning("Ignoring uplink without device ID or decoded payload")
        return None

    fields = {}
    for source_name, field_name in PAYLOAD_FIELDS.items():
        value = as_number(payload.get(source_name))
        if value is not None:
            fields[field_name] = value
    if not fields:
        LOGGER.warning("Ignoring uplink from %s without known numeric fields", device_id)
        return None

    try:
        timestamp = parse_received_at(uplink.get("received_at")).isoformat()
    except ValueError as error:
        LOGGER.warning("Ignoring uplink from %s with invalid received_at: %s", device_id, error)
        return None
    return {
        "measurement": MEASUREMENT,
        "device_id": device_id,
        "fields": fields,
        "timestamp": timestamp,
    }


def record_to_point(record: dict[str, Any]) -> Point:
    point = Point(record["measurement"]).tag("device_id", record["device_id"])
    for name, value in record["fields"].items():
        point.field(name, value)
    point.time(record["timestamp"])
    return point


def build_point(message: dict[str, Any]) -> Point | None:
    record = message_to_record(message)
    return record_to_point(record) if record else None


def quality_flags(fields: dict[str, float]) -> tuple[bool, bool]:
    """Return (all_zero, out_of_range) without changing accepted telemetry."""
    all_zero = len(fields) == len(PAYLOAD_FIELDS) and all(value == 0 for value in fields.values())
    out_of_range = any(
        name in QUALITY_LIMITS and not (QUALITY_LIMITS[name][0] <= value <= QUALITY_LIMITS[name][1])
        for name, value in fields.items()
    )
    if "wind_speed" in fields and "gust" in fields and fields["gust"] < fields["wind_speed"]:
        out_of_range = True
    return all_zero, out_of_range


class Counters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mqtt_connected = 0
        self.rx_total = 0
        self.stored_total = 0
        self.rejected_total = 0
        self.write_fail_total = 0
        self.suspect_total = 0
        self.out_of_range_total = 0
        self.last_rx_at = 0.0
        self.last_stored_at = 0.0

    def increment(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    def mark_received(self) -> None:
        with self._lock:
            self.rx_total += 1
            self.last_rx_at = time.monotonic()

    def mark_stored(self) -> None:
        with self._lock:
            self.stored_total += 1
            self.last_stored_at = time.monotonic()

    def mark_quality(self, all_zero: bool, out_of_range: bool) -> None:
        with self._lock:
            if all_zero:
                self.suspect_total += 1
            if out_of_range:
                self.out_of_range_total += 1

    def snapshot(self, spool_depth: int) -> dict[str, float]:
        with self._lock:
            return {
                "mqtt_connected": self.mqtt_connected,
                "rx_total": self.rx_total,
                "stored_total": self.stored_total,
                "rejected_total": self.rejected_total,
                "write_fail_total": self.write_fail_total,
                "suspect_total": self.suspect_total,
                "out_of_range_total": self.out_of_range_total,
                "spool_depth": spool_depth,
            }


def spool_depth(path: Path = SPOOL_PATH) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.open(encoding="utf-8") if line.strip())


def append_to_spool(record: dict[str, Any], path: Path = SPOOL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as spool:
        spool.write(json.dumps(record, separators=(",", ":")) + "\n")


def retry_write(write_api: Any, *, bucket: str, org: str, point: Point, counters: Counters) -> bool:
    delay = INITIAL_RETRY_DELAY
    for attempt in range(MAX_WRITE_ATTEMPTS):
        try:
            write_api.write(bucket=bucket, org=org, record=point)
            return True
        except Exception:
            counters.increment("write_fail_total")
            if attempt == MAX_WRITE_ATTEMPTS - 1:
                LOGGER.exception("InfluxDB write failed after %s attempts", MAX_WRITE_ATTEMPTS)
            else:
                LOGGER.warning("InfluxDB write failed; retrying in %.1fs", delay)
                time.sleep(delay)
                delay *= 2
    return False


def replay_spool(write_api: Any, *, bucket: str, org: str, path: Path, counters: Counters) -> None:
    if not path.exists():
        return
    records = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    remaining = []
    for record in records:
        if not retry_write(write_api, bucket=bucket, org=org, point=record_to_point(record), counters=counters):
            remaining.append(record)
        else:
            counters.increment("stored_total")
    if remaining:
        path.write_text("\n".join(json.dumps(record, separators=(",", ":")) for record in remaining) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def health_point(counters: Counters, path: Path) -> Point:
    point = Point(HEALTH_MEASUREMENT)
    for name, value in counters.snapshot(spool_depth(path)).items():
        point.field(name, value)
    return point


class AlertState:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.spool_started_at = 0.0
        self.last_rejected_total = 0
        self.last_quality_total = 0

    def transition(self, name: str, condition: bool, message: str) -> None:
        if condition and name not in self.active:
            LOGGER.error("ALERT: %s", message)
            self.active.add(name)
        elif not condition and name in self.active:
            LOGGER.info("ALERT CLEARED: %s", name.replace("_", " "))
            self.active.remove(name)


def check_alerts(counters: Counters, path: Path, state: AlertState) -> None:
    now = time.monotonic()
    snapshot = counters.snapshot(spool_depth(path))
    stale_after = EXPECTED_INTERVAL_SECONDS * STALE_MISSED_CYCLES
    no_telemetry = counters.last_stored_at == 0 or now - counters.last_stored_at > stale_after
    state.transition(
        "mqtt_disconnected",
        snapshot["mqtt_connected"] == 0,
        "MQTT is disconnected",
    )
    state.transition(
        "stale_telemetry",
        no_telemetry,
        f"no stored telemetry for more than {stale_after:.0f}s",
    )
    if snapshot["spool_depth"] > 0:
        if state.spool_started_at == 0:
            state.spool_started_at = now
        spool_delayed = now - state.spool_started_at >= SPOOL_ALERT_SECONDS
    else:
        state.spool_started_at = 0
        spool_delayed = False
    state.transition(
        "spool_backlog",
        spool_delayed,
        f"spool contains {int(snapshot['spool_depth'])} point(s) for at least {SPOOL_ALERT_SECONDS:.0f}s",
    )
    state.transition(
        "rejected_packets",
        snapshot["rejected_total"] > state.last_rejected_total,
        f"{int(snapshot['rejected_total'] - state.last_rejected_total)} new uplink(s) have been rejected",
    )
    state.last_rejected_total = int(snapshot["rejected_total"])
    quality_total = int(snapshot["suspect_total"] + snapshot["out_of_range_total"])
    state.transition(
        "suspect_samples",
        quality_total > state.last_quality_total,
        f"new quality issue(s): suspect={int(snapshot['suspect_total'])}, "
        f"out_of_range={int(snapshot['out_of_range_total'])}",
    )
    state.last_quality_total = quality_total


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app_id = required_setting("TTN_APP_ID")
    api_key = required_setting("TTN_API_KEY")
    influx_token = required_setting("INFLUXDB_TOKEN")
    broker = os.getenv("TTN_BROKER", "eu1.cloud.thethings.network")
    port = int(os.getenv("TTN_PORT", "8883"))
    influx_url = os.getenv("INFLUX_URL", "http://influxdb:8086")
    organization = os.getenv("INFLUXDB_ORG", "atmos")
    bucket = os.getenv("INFLUXDB_BUCKET", "wind_telemetry")
    topic = f"v3/{app_id}@ttn/devices/+/up"
    counters = Counters()

    influx = InfluxDBClient(url=influx_url, token=influx_token, org=organization)
    write_api = influx.write_api(write_options=SYNCHRONOUS)
    stop_health = threading.Event()
    alert_state = AlertState()

    def health_loop() -> None:
        while not stop_health.wait(60):
            replay_spool(write_api, bucket=bucket, org=organization, path=SPOOL_PATH, counters=counters)
            retry_write(
                write_api,
                bucket=bucket,
                org=organization,
                point=health_point(counters, SPOOL_PATH),
                counters=counters,
            )
            check_alerts(counters, SPOOL_PATH, alert_state)

    def on_connect(client: mqtt.Client, _userdata: object, _flags: dict, reason_code: mqtt.ReasonCode, _properties: object = None) -> None:
        if reason_code.is_failure:
            counters.mqtt_connected = 0
            LOGGER.error("TTN MQTT connection failed: %s", reason_code)
            return
        counters.mqtt_connected = 1
        result, _ = client.subscribe(topic)
        if result != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Could not subscribe to %s: %s", topic, result)
            return
        LOGGER.info("Connected to TTN and subscribed to %s", topic)

    def on_disconnect(_client: mqtt.Client, _userdata: object, _disconnect_flags: object, reason_code: mqtt.ReasonCode, _properties: object = None) -> None:
        counters.mqtt_connected = 0
        LOGGER.warning("Disconnected from TTN MQTT: %s; waiting for automatic reconnect", reason_code)

    def on_message(_client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        counters.mark_received()
        try:
            decoded = json.loads(message.payload.decode("utf-8"))
            record = message_to_record(decoded)
            if record is None:
                counters.increment("rejected_total")
                return
            all_zero, out_of_range = quality_flags(record["fields"])
            counters.mark_quality(all_zero, out_of_range)
            if not retry_write(write_api, bucket=bucket, org=organization, point=record_to_point(record), counters=counters):
                append_to_spool(record)
                LOGGER.error("Spooled telemetry from %s for later retry", record["device_id"])
                return
            counters.mark_stored()
            LOGGER.info("Stored telemetry from %s", record["device_id"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            counters.increment("rejected_total")
            LOGGER.warning("Ignoring malformed uplink: %s", error)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(f"{app_id}@ttn", api_key)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    if port == 8883:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    LOGGER.info("Connecting to TTN MQTT broker %s:%s", broker, port)
    health_thread = threading.Thread(target=health_loop, name="bridge-health", daemon=True)
    health_thread.start()
    try:
        replay_spool(write_api, bucket=bucket, org=organization, path=SPOOL_PATH, counters=counters)
        client.connect(broker, port, keepalive=60)
        client.loop_forever(retry_first_connection=True)
    finally:
        stop_health.set()
        health_thread.join(timeout=2)
        write_api.close()
        influx.close()


if __name__ == "__main__":
    main()
