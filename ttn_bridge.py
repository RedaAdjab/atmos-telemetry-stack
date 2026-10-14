import json
import logging
import math
import os
import ssl

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()

LOGGER = logging.getLogger("ttn_bridge")
MEASUREMENT = "wind_telemetry"
PAYLOAD_FIELDS = {
    "wind_speed": "wind_speed",
    "wind_direction": "wind_direction",
    "gust": "gust",
    "temperature": "temperature",
}


def required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("your-") or value.startswith("change-this"):
        raise RuntimeError(f"Set {name} in .env before starting the bridge")
    return value


def as_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_point(message: dict) -> Point | None:
    device_id = message.get("end_device_ids", {}).get("device_id")
    payload = message.get("uplink_message", {}).get("decoded_payload")
    if not device_id or not isinstance(payload, dict):
        LOGGER.warning("Ignoring uplink without device ID or decoded payload")
        return None

    point = Point(MEASUREMENT).tag("device_id", device_id)
    field_count = 0
    for source_name, field_name in PAYLOAD_FIELDS.items():
        value = as_number(payload.get(source_name))
        if value is not None:
            point.field(field_name, value)
            field_count += 1

    if field_count == 0:
        LOGGER.warning("Ignoring uplink from %s without known numeric fields", device_id)
        return None
    return point


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

    influx = InfluxDBClient(url=influx_url, token=influx_token, org=organization)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    def on_connect(client: mqtt.Client, _userdata: object, _flags: dict, reason_code: mqtt.ReasonCode, _properties: object = None) -> None:
        if reason_code.is_failure:
            LOGGER.error("TTN MQTT connection failed: %s", reason_code)
            return
        result, _ = client.subscribe(topic)
        if result != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Could not subscribe to %s: %s", topic, result)
            return
        LOGGER.info("Connected to TTN and subscribed to %s", topic)

    def on_message(_client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        try:
            decoded = json.loads(message.payload.decode("utf-8"))
            point = build_point(decoded)
            if point is None:
                return
            write_api.write(bucket=bucket, org=organization, record=point)
            device_id = decoded["end_device_ids"]["device_id"]
            LOGGER.info("Stored telemetry from %s", device_id)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            LOGGER.warning("Ignoring malformed uplink: %s", error)
        except Exception:
            LOGGER.exception("Could not write telemetry to InfluxDB")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(f"{app_id}@ttn", api_key)
    if port == 8883:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect
    client.on_message = on_message

    LOGGER.info("Connecting to TTN MQTT broker %s:%s", broker, port)
    try:
        client.connect(broker, port, keepalive=60)
        client.loop_forever()
    finally:
        write_api.close()
        influx.close()


if __name__ == "__main__":
    main()
