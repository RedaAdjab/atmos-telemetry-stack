# 1. Architecture and Fundamentals

This project is an IoT telemetry pipeline. A sensor measures something, the measurement travels over a network, a program stores it, and a dashboard displays it.

The complete path is:

```text
STM32 sensor node
  -> LoRaWAN radio packet
  -> Laird gateway
  -> TTN LoRaWAN network server
  -> TTN MQTT broker
  -> ttn_bridge.py
  -> InfluxDB
  -> Grafana
  -> browser dashboard
```

## 1.1 The physical and cloud parts

### STM32 node

The STM32 is the device that measures wind speed, wind direction, and battery level. It sends a LoRaWAN uplink at a chosen interval, for example every 10 seconds.

The node normally sends compact encoded bytes, not a ready-to-read JSON document. TTN receives those bytes and uses the application's payload formatter to decode them.

### Laird gateway

A gateway is a radio-to-IP bridge. It receives LoRa radio traffic from nearby nodes and forwards it to TTN over the Internet. The gateway does not normally decide what a measurement means; TTN and the application decoder handle that.

The gateway may use UDP port 1700 to communicate with a network server. That is separate from the MQTT connection used by this project.

### The Things Network (TTN)

TTN is the cloud LoRaWAN network server. It authenticates devices, receives uplinks, attaches metadata, and publishes application events to an MQTT broker.

An **application** is a logical TTN project containing one or more end devices. The value in `.env` called `TTN_APP_ID` is that application ID, not the device ID.

An **API key** is a secret credential. This project needs permission to read application traffic. Treat it like a password and never commit it to a repository or paste it into a public issue.

## 1.2 MQTT in simple terms

MQTT is a lightweight publish/subscribe messaging protocol.

- A **publisher** sends a message to a named topic.
- A **subscriber** listens to one or more topics.
- A **broker** receives messages and forwards them to subscribers.
- A **topic** is a slash-separated routing name, similar to a folder path.

The bridge is an MQTT subscriber. TTN publishes uplink JSON messages, and the bridge receives them without repeatedly asking TTN whether anything changed. This is why MQTT is useful for sensor streams.

### The TTN topic used here

The bridge subscribes to:

```text
v3/<application-id>@ttn/devices/+/up
```

For example, if `TTN_APP_ID=stm32-lorawan-lab-reda`, the topic is:

```text
v3/stm32-lorawan-lab-reda@ttn/devices/+/up
```

The `+` is an MQTT single-level wildcard. It matches any device ID, so one bridge can receive uplinks from every device in the application. The final `up` means an uplink from a device to the network.

The username is `TTN_APP_ID@ttn`; the API key is the MQTT password. Port `8883` means MQTT over TLS. Port `1883` is unencrypted MQTT and should not be used for a real deployment unless the broker and network are explicitly secured another way.

## 1.3 Why InfluxDB is used

InfluxDB is a time-series database. It is designed for values that change over time, such as wind speed every 10 seconds.

This bridge writes points shaped like this:

```text
measurement: wind_telemetry
tag:         device_id = stm32-b-l072z-node
fields:      wind_speed_ms = 5.2
             wind_direction_deg = 180.5
             battery_percent = 100
timestamp:   assigned by InfluxDB when the point is written
```

Important InfluxDB concepts:

- A **measurement** is similar to a table name. Here it is `wind_telemetry`.
- A **tag** is indexed metadata used for filtering and grouping. `device_id` is a good tag because dashboards often filter by device.
- A **field** is the measured value. Wind speed, direction, and battery are fields.
- A **timestamp** says when the point belongs in the time series. InfluxDB assigns one when the bridge writes the point because this code does not provide a device timestamp.
- A **bucket** is a named storage area with a retention policy. This project uses `telemetry`.
- An **organization** groups InfluxDB resources. This project uses `cder_lab`.
- A **token** authorizes API access. The bridge and Grafana use a token to read or write data.

Do not store rapidly changing measurements as tags. A unique numeric value as a tag creates high cardinality and can make a time-series database slower. Measurements belong in fields.

## 1.4 Why Grafana is used

Grafana is the visualization layer. It connects to InfluxDB, runs queries, and renders panels such as time-series charts, gauges, tables, and stat cards.

Grafana does not receive TTN messages directly in this design. It reads data after the Python bridge has stored it. This separation makes each component easier to replace and troubleshoot:

1. Check TTN/MQTT when messages do not arrive.
2. Check Python logs when messages arrive but are not stored.
3. Check InfluxDB when storage or queries fail.
4. Check Grafana when data exists but the dashboard is wrong.

## 1.5 Containers, localhost, and ports

Docker runs InfluxDB and Grafana in isolated containers. A container is a packaged process with its dependencies and filesystem separated from the host.

The Compose file maps ports like this:

```text
host port 8086 -> InfluxDB container port 8086
host port 3000 -> Grafana container port 3000
```

Therefore:

- A browser on the PC uses `http://localhost:8086` and `http://localhost:3000`.
- The Python process running directly on the PC uses `http://localhost:8086`.
- Grafana running inside Docker uses the Compose service name `http://influxdb:8086`, because containers use an internal Docker network.

`localhost` always means "this machine or this container." It does not mean "the computer running some other service." This is the reason the same InfluxDB service has two different URLs depending on where the client runs.

## 1.6 What an API is in this project

An API is a defined way for software to communicate. There are several APIs here:

- TTN MQTT API: a stream of messages delivered through topics.
- InfluxDB HTTP API: used by the Python client and Grafana to write/query data.
- Grafana web API/UI: used by the browser and optionally by automation.

The word API does not necessarily mean a web page. MQTT topics, client libraries, and HTTP endpoints are all interfaces with rules about authentication, data format, and errors.

## 1.7 The key contract: decoded payload names

The bridge expects TTN's decoded JSON to contain these names:

```json
{
  "wind_speed_ms": 5.2,
  "wind_direction_deg": 180.5,
  "battery_percent": 100
}
```

The names must match the TTN payload formatter and the code. If firmware sends `windSpeed` but the formatter produces `wind_speed_ms`, the bridge is fine. If the formatter produces neither expected name, the bridge will store no recognized value.

This is an interface contract between firmware, TTN decoding, Python, InfluxDB, and Grafana. When changing a field name, update and test every layer.