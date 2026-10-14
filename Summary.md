# Comprehensive Technical Summary: TTN MQTT, Python Bridge, InfluxDB & Grafana Integration
 
This document reflects the verified software-side implementation of the ATMOS wind telemetry deployment. It follows the data after the STM32L073RZ firmware creates a LoRaWAN uplink: from the Laird gateway and The Things Network (TTN), through the TTN MQTT broker and containerized Python bridge, into InfluxDB time-series storage and Grafana visualization.
 
The hardware/firmware summary describes how the measurement is acquired and encoded. This summary describes how that encoded uplink becomes a validated time-series record and an operator-facing dashboard.
 
## 1. End-to-End Software Topology
 
The complete operational path is:
 
```text
+----------------------+   LoRaWAN   +------------------+   Internet   +-------------------------+
| STM32L073RZ + SX1272| ----------> | Laird Gateway   | -----------> | The Things Network     |
| Firmware uplink      |             | Radio/IP bridge |              | LoRaWAN network server|
+----------------------+             +------------------+              +-----------+-------------+
                                                                                   |
                                                                        TTN MQTT over TLS
                                                                                   |
                                                                                   v
                                                                        +----------------------+
                                                                        | Python TTN Bridge    |
                                                                        | ttn_bridge.py       |
                                                                        | MQTT -> InfluxDB    |
                                                                        +----------+-----------+
                                                                                   |
                                                               HTTP inside Docker network
                                                                                   |
                                                                                   v
                                                                        +----------------------+
                                                                        | InfluxDB 2.7         |
                                                                        | wind_telemetry       |
                                                                        +----------+-----------+
                                                                                   |
                                                               Flux queries over HTTP
                                                                                   |
                                                                                   v
                                                                        +----------------------+
                                                                        | Grafana 11.6.0       |
                                                                        | Dashboards / panels  |
                                                                        +----------+-----------+
                                                                                   |
                                                                                   v
                                                                        Browser on host machine
```
 
The Python bridge is the adapter between two different interfaces:
 
- TTN publishes JSON uplink events through MQTT topics.
- InfluxDB accepts timestamped measurements through its HTTP client API.
- Grafana queries InfluxDB and renders the stored values; it does not consume TTN MQTT messages directly.
 
The current deployment runs all three software services through Docker Compose: InfluxDB, Grafana, and the bridge. The browser reaches Grafana and InfluxDB through published host ports, while containers reach each other through Docker service names.
 
## 2. Verified Service and Interface Matrix
 
| Component | Current implementation | Primary interface | Persistent state |
| --- | --- | --- | --- |
| TTN MQTT broker | Regional TTN broker, normally `eu1.cloud.thethings.network` | MQTT over TLS, port `8883` | Managed by TTN |
| `bridge` | Python 3.12-slim container built from `Dockerfile.bridge` | MQTT subscription and InfluxDB HTTP client | No local application data volume |
| `influxdb` | `influxdb:2.7` | HTTP on container port `8086` | `influxdb_data`, `influxdb_config` |
| `grafana` | `grafana/grafana:11.6.0` | HTTP on container port `3000` | `grafana_data` plus read-only provisioning mounts |
| Host browser | Windows host | `http://localhost:3000` and `http://localhost:8086` | None |
 
The Compose file publishes `8086:8086` for InfluxDB and `3000:3000` for Grafana. The bridge does not publish a host port because it only needs outbound MQTT access and internal access to InfluxDB.
 
Within the Compose network, the bridge uses `http://influxdb:8086`, and Grafana uses the same service URL. `localhost` refers to the current container, so it must not be used for container-to-container communication.
 
## 3. TTN MQTT Integration Contract
 
### 3.1 Broker authentication
 
The bridge loads the following settings from the environment:
 
- `TTN_BROKER`: regional TTN MQTT hostname.
- `TTN_PORT`: MQTT port, defaulting to `8883`.
- `TTN_APP_ID`: TTN application identifier.
- `TTN_API_KEY`: TTN credential with permission to read application uplink traffic.
 
The MQTT username is constructed as:
 
```text
<TTN_APP_ID>@ttn
```
 
The API key is supplied as the MQTT password. When `TTN_PORT` is `8883`, the bridge calls `tls_set(cert_reqs=ssl.CERT_REQUIRED)`, so the broker certificate is verified rather than accepting an untrusted connection.
 
### 3.2 Uplink topic
 
The bridge subscribes to exactly one application-wide topic:
 
```text
v3/<application-id>@ttn/devices/+/up
```
 
For example:
 
```text
v3/atmos-wind-app@ttn/devices/+/up
```
 
The `+` is an MQTT single-level wildcard. It allows the same bridge to receive uplinks from every device in the configured TTN application. The bridge later reads the actual device ID from the JSON event and stores it as an InfluxDB tag.
 
The final `up` identifies an uplink event travelling from a device toward the application. Downlinks, join events, and unrelated application events are outside this subscription.
 
### 3.3 MQTT client lifecycle
 
`ttn_bridge.py` creates a Paho MQTT client using `CallbackAPIVersion.VERSION2`. It registers two callbacks:
 
- `on_connect()`: subscribes only after the broker accepts the connection.
- `on_message()`: decodes each received MQTT payload and writes valid telemetry.
- MQTT reconnects automatically after a disconnect, with a bounded reconnect delay.
 
`loop_forever()` maintains the MQTT network loop. It receives packets, services keepalive traffic, and dispatches callbacks. The bridge remains a long-running ingestion process until it is stopped or an unrecoverable process-level failure occurs.
 
## 4. JSON Payload and Field Contract
 
The bridge expects TTN to publish a decoded payload at:
 
```text
end_device_ids.device_id
uplink_message.decoded_payload
```
 
The `decoded_payload` object is expected to contain these numeric names:
 
```json
{
  "wind_speed": 5.2,
  "wind_direction": 180.5,
  "gust": 7.1,
  "temperature": 24.3
}
```
 
These names are the software contract between the TTN payload formatter and the Python bridge. They are not inferred from the binary firmware payload at this stage; TTN must already have decoded the eight-byte LoRaWAN application payload into these fields.
 
The bridge maps the fields as follows:
 
| TTN decoded field | InfluxDB field | Meaning and expected scale |
| --- | --- | --- |
| `wind_speed` | `wind_speed` | Wind speed in m/s, produced from the firmware scale of m/s x 100 |
| `wind_direction` | `wind_direction` | Direction in degrees, produced from the firmware scale of degrees x 10 |
| `gust` | `gust` | Gust speed in m/s, produced from the firmware scale of m/s x 100 |
| `temperature` | `temperature` | Temperature in degrees C, produced from the signed firmware scale of degrees C x 10 |
 
A field name change must be coordinated across the firmware decoder, TTN formatter, bridge, InfluxDB queries, and Grafana panels. A valid MQTT message with mismatched names can be received successfully but still produce no stored telemetry fields.
 
## 5. Python Bridge Processing Engine
 
### 5.1 Configuration and startup validation
 
`load_dotenv()` loads values from `.env` for local execution. In Compose, the service receives the configured values through the `environment` section of `docker-compose.yml`.
 
The bridge requires non-placeholder values for:
 
- `TTN_APP_ID`
- `TTN_API_KEY`
- `INFLUXDB_TOKEN`
 
`required_setting()` rejects empty values and common placeholders beginning with `your-` or `change-this`. This fails early before network clients are created, preventing a misleading startup with an empty credential.
 
The bridge uses these defaults when optional values are absent:
 
```text
TTN_BROKER   = eu1.cloud.thethings.network
TTN_PORT     = 8883
INFLUX_URL   = http://influxdb:8086
INFLUXDB_ORG = atmos
INFLUXDB_BUCKET = wind_telemetry
LOG_LEVEL    = INFO
```
 
### 5.2 Message decoding and rejection rules
 
For every MQTT message, `on_message()` performs this sequence:
 
1. Decode the MQTT byte payload as UTF-8.
2. Parse the result as JSON.
3. Extract the TTN device ID.
4. Extract `uplink_message.decoded_payload`.
5. Convert each known field to a finite floating-point number.
6. Build an InfluxDB point if at least one known numeric field exists.
7. Use `uplink_message.received_at` as the point timestamp.
8. Write the point synchronously with retry and exponential backoff; permanently failed points are spooled for replay.
9. Log the device ID after a successful write.
 
Messages are ignored when the device ID is missing, the decoded payload is not an object, or no recognized field contains a valid finite number. The bridge does not insert zeroes for missing fields. This preserves the distinction between a real zero measurement and an absent measurement.
 
`as_number()` rejects values that cannot become numbers and values such as `NaN` or infinity. Each field is validated independently, so a packet with one valid value and several missing or malformed values can still be stored with the valid field only.

The bridge also performs non-blocking quality checks after numeric validation.
It keeps the raw point but increments `suspect_total` when all four fields are
zero, and increments `out_of_range_total` for broad physical-limit violations
or when gust is lower than wind speed.
 
### 5.3 Error handling and logging
 
Expected malformed-input errors, including invalid UTF-8, invalid JSON, missing keys, and incompatible types, are logged as warnings and discarded. Unexpected exceptions, especially InfluxDB write failures, are logged with a traceback.
 
The callback catches these errors so that one malformed packet or failed write does not terminate the entire MQTT process. The operational logs provide two important success markers:
 
```text
Connecting to TTN MQTT broker <broker>:<port>
Connected to TTN and subscribed to v3/<application-id>@ttn/devices/+/up
Stored telemetry from <device-id>
```
 
### 5.4 Synchronous database writes
 
The bridge creates an `InfluxDBClient` with the configured URL, token, and organization, then obtains a synchronous write API. A successful `write_api.write()` call completes before the bridge logs `Stored telemetry`.
 
This approach is simple and appropriate for the current low-volume sensor deployment. Failed writes are retried and then spooled so a short InfluxDB outage does not lose accepted uplinks. A `bridge_health` point is written every 60 seconds with connection, ingestion, rejection, write-failure, spool-depth, and data-quality counters. The bridge logs transition-based alerts for MQTT disconnects, stale telemetry, spool backlog, newly rejected packets, and newly detected quality issues. A process that is completely down cannot print its own alert; external observation of missing health points remains the appropriate way to detect that condition.
 
The write API and InfluxDB client are closed in the `finally` block when the MQTT loop exits.

### 5.5 Operational alerts

The bridge writes a `bridge_health` point every 60 seconds and prints
transition-based alerts to its normal log output. The current alerts cover:

- MQTT disconnection.
- Stale telemetry: no successfully stored point for the configured threshold.
- A spool backlog that remains present for the configured threshold.
- Newly rejected uplinks.
- Newly detected quality issues.

The default stale-data settings are:

```text
EXPECTED_INTERVAL_SECONDS = 120
STALE_MISSED_CYCLES        = 3
```

Therefore, the default stale alert threshold is six minutes without a
successfully stored telemetry point. `SPOOL_ALERT_SECONDS` defaults to five
minutes. These values must be adjusted to the device's real production
cadence; the nominal firmware transmit loop is not necessarily the actual
LoRaWAN delivery cadence. Alerts can be viewed with:

```text
docker compose logs -f bridge
```

The alerts are deliberately log-based and do not modify Grafana dashboards.
A completely stopped bridge cannot print its own alert, so missing
`bridge_health` data remains the signal for an external monitor or a future
Grafana alert.
 
## 6. InfluxDB Data Model
 
Every accepted packet is written using this logical schema:
 
```text
measurement: wind_telemetry
tag:         device_id = <TTN device ID>
fields:      wind_speed       = <number, when present>
             wind_direction   = <number, when present>
             gust             = <number, when present>
             temperature      = <number, when present>
time:        TTN uplink_message.received_at
```
 
The bridge supplies TTN's `uplink_message.received_at`, so replaying a spooled point preserves the time at which TTN received the uplink rather than moving it to the replay time.

Quality checks do not reject raw telemetry. All-zero samples increment
`suspect_total`; broad physical-limit violations and gust values below wind
speed increment `out_of_range_total`. Thresholds are intentionally broad and
are reported through `bridge_health` and bridge logs rather than changing
existing Grafana panels.
 
`device_id` is stored as a tag because it is metadata used for filtering and grouping. Sensor values remain fields because they change frequently and should not create high-cardinality tag values.
 
The active database configuration comes from `.env`:
 
- `INFLUXDB_ORG`: organization that owns the bucket.
- `INFLUXDB_BUCKET`: bucket receiving telemetry.
- `INFLUXDB_TOKEN`: token used by the bridge and Grafana.
- `INFLUXDB_USERNAME` and `INFLUXDB_PASSWORD`: initial administrator credentials used by the InfluxDB image during first setup.
 
The initialization variables apply when the database is first created. Existing named volumes retain their original organization, bucket, credentials, and token state; changing `.env` later does not reinitialize an existing database.
 
## 7. Docker Compose Runtime Architecture
 
### 7.1 InfluxDB service
 
The `influxdb` service uses `influxdb:2.7`, restarts with `unless-stopped`, and exposes port `8086` to the Windows host. Its health check runs `influx ping` every ten seconds, with twelve retries and a five-second timeout.
 
Two named volumes preserve its state:
 
```text
influxdb_data   -> /var/lib/influxdb2
influxdb_config -> /etc/influxdb2
```
 
### 7.2 Grafana service
 
The `grafana` service uses `grafana/grafana:11.6.0`, exposes port `3000`, and starts only after InfluxDB reports healthy. Its administrator credentials come from `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD`.
 
Grafana stores its internal state in:
 
```text
grafana_data -> /var/lib/grafana
```
 
Provisioning files and dashboard definitions are mounted read-only from the repository. The current dashboard directory is available to Grafana at `/var/lib/grafana/dashboards`.
 
### 7.3 Bridge service
 
The `bridge` service is built locally from `Dockerfile.bridge`:
 
```dockerfile
FROM python:3.12-slim
```
 
The image installs the pinned packages in `requirements.txt`, copies `ttn_bridge.py` into `/app`, and starts it with `python ttn_bridge.py`.
 
The pinned runtime dependencies are:
 
```text
influxdb-client==1.48.0
paho-mqtt==2.1.0
python-dotenv==1.1.0
```
 
The bridge depends on the InfluxDB health check before starting. It has `restart: unless-stopped`, which allows Docker to restart it after a container failure or host restart while preserving the service's stateless design.
 
## 8. Grafana Provisioning and Visualization
 
### 8.1 InfluxDB data source
 
Grafana receives its data source from `grafana/provisioning/datasources/influxdb.yml`. The provisioned data source has:
 
```text
name:             InfluxDB
uid:              influxdb
type:             influxdb
access:            proxy
url:              http://influxdb:8086
query language:   Flux
organization:     ${INFLUXDB_ORG}
default bucket:   ${INFLUXDB_BUCKET}
TLS verification: enabled
```
 
The token is supplied through `secureJsonData`, so Grafana can authenticate to InfluxDB without placing the token in a dashboard query. `access: proxy` means Grafana performs the query from inside the Grafana container, which is why the internal service URL is correct.
 
### 8.2 Dashboard provider
 
`grafana/provisioning/dashboards/dashboards.yml` defines a file-based provider named `Atmos dashboards` in the `Atmos` folder. It scans:
 
```text
/var/lib/grafana/dashboards
```
 
The provider checks for changes every 30 seconds, permits UI updates, and does not disable deletion. The repository's dashboard directory is currently empty, so there are no dashboard JSON definitions under source control at this time. Dashboards created through the Grafana UI are retained in the `grafana_data` named volume.
 
This creates two distinct dashboard paths:
 
- Provisioned dashboards: repeatable files mounted from the repository.
- UI-created dashboards: stored in Grafana's persistent application volume.
 
### 8.3 Query and field behavior
 
A Flux query for stored wind speed follows this shape:
 
```flux
from(bucket: "wind_telemetry")
  |> range(start: -1h)
  |> filter(fn: (r) => r["_measurement"] == "wind_telemetry")
  |> filter(fn: (r) => r["_field"] == "wind_speed")
```
 
A device-specific query can additionally filter the `device_id` tag:
 
```flux
|> filter(fn: (r) => r["device_id"] == "<device-id>")
```
 
Because missing fields are not written, Grafana should display an absent value as no data rather than interpreting it as zero. An empty panel must first be checked against its time range, bucket, measurement, field, device filter, and data source.
 
## 9. Persistence, Lifecycle, and Access Boundaries
 
The normal lifecycle commands are:
 
```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f bridge
docker compose down
```
 
A normal `docker compose down` removes containers but keeps the named volumes. `docker compose down -v` removes the named volumes and can delete all stored InfluxDB and Grafana data. Docker volumes provide persistence, not a complete backup strategy.
 
The host-facing URLs are:
 
```text
Grafana:  http://localhost:3000
InfluxDB: http://localhost:8086
```
 
These addresses are local to the Windows machine. They do not make the dashboard publicly reachable. LAN or Internet hosting would require deliberate firewall rules, authentication, HTTPS, backups, and preferably VPN or reverse-proxy access. InfluxDB's administrative port should not be exposed publicly without a complete security design.
 
## 10. Verified Operational Telemetry Sequence
 
```text
[ STM32 firmware creates LoRaWAN uplink ]
                  |
                  v
[ Laird gateway receives radio packet ]
                  |
                  v
[ TTN authenticates, processes, and decodes uplink ]
                  |
                  v
[ TTN publishes JSON to MQTT topic ]
  v3/<application-id>@ttn/devices/+/up
                  |
                  v
[ Python bridge on_connect() ]
  -> Authenticate with <application-id>@ttn and API key
  -> Verify TLS when using port 8883
  -> Subscribe to application uplink topic
                  |
                  v
[ Python bridge on_message() ]
  -> Decode UTF-8 JSON
  -> Read end_device_ids.device_id
  -> Read uplink_message.decoded_payload
  -> Convert known values to finite numbers
  -> Reject malformed or fieldless packets
                  |
                  v
[ InfluxDB point creation ]
  -> Measurement: wind_telemetry
  -> Tag: device_id
  -> Fields: wind_speed, wind_direction, gust, temperature
  -> Timestamp: assigned during write
                  |
                  v
[ Synchronous write to InfluxDB bucket ]
                  |
                  v
[ Grafana queries InfluxDB through Docker network ]
                  |
                  v
[ Browser renders time-series panels, gauges, and tables ]
```
 
## 11. Fault Boundaries and Diagnostic Order
 
The pipeline has separate failure boundaries. Diagnose them in order rather than changing dashboard queries first:
 
1. **Device and gateway:** confirm that the STM32 is transmitting and the Laird gateway is receiving traffic.
2. **TTN:** confirm that the application receives an uplink and shows the expected decoded payload.
3. **MQTT authentication:** confirm the regional broker, application ID, API key permissions, username format, port, and TLS connection.
4. **Bridge subscription:** confirm a log line showing the exact `v3/.../devices/+/up` subscription.
5. **Payload contract:** confirm `device_id`, `decoded_payload`, and the four expected numeric field names.
6. **InfluxDB write:** look for `Stored telemetry from ...` and check the configured organization and bucket.
7. **Grafana data source:** confirm Grafana uses `http://influxdb:8086`, Flux, the correct organization, bucket, and token.
8. **Dashboard query:** confirm the time range, measurement `wind_telemetry`, field name, and optional `device_id` filter.
 
Typical symptoms map to the following causes:
 
- No MQTT connection: broker, network, credentials, or TLS configuration.
- Connected but no stored records: no uplinks, wrong topic/application, missing decoded payload, or field-name mismatch.
- `Stored telemetry` appears but Grafana is empty: wrong bucket, organization, time range, data source, measurement, or field filter.
- Some values are missing: those fields were absent or invalid in the decoded payload and were intentionally not written.
- Data disappears after `down -v`: named volumes were explicitly deleted.
 
## 12. Software-Side Integration Contract with Firmware
 
The firmware summary defines an eight-byte big-endian application payload:
 
```text
bytes 0-1: wind speed      = m/s x 100, unsigned
bytes 2-3: wind direction  = degrees x 10, unsigned
bytes 4-5: gust speed       = m/s x 100, unsigned
bytes 6-7: temperature      = degrees C x 10, signed
```
 
The software pipeline begins after TTN's application payload formatter reverses those scales and emits the JSON names consumed by the bridge. The boundary is therefore:
 
```text
Firmware binary payload
  -> TTN decoder / decoded_payload
  -> MQTT JSON event
  -> Python numeric validation
  -> InfluxDB fields
  -> Grafana Flux queries
```
 
The system is end-to-end correct only when these representations agree. A radio packet can be received successfully while still producing no database point if TTN does not create `decoded_payload`, or if its field names do not match the bridge's four-name contract.
 
## 13. Current Deployment Scope and Future Operational Considerations
 
The current project is a working local/containerized telemetry deployment. It provides persistent database and Grafana state, TLS MQTT transport, startup ordering for InfluxDB-dependent services, numeric validation, and clear logging at the ingestion boundary.
 
The implementation now stores bridge-health counters, retries and spools failed writes, provisions a dashboard JSON, and provides manual backup plus restore verification scripts. The raw bucket is configured for 90-day retention on first initialization.
 
For a larger or unattended deployment, the natural next additions are:
 
- Add a downsampling policy for long-term aggregates.
- Add alerts and external/off-site backup storage.
- Add TTN backfill support for outages longer than the local spool window.
- Add externally evaluated alerts for missing `bridge_health` points when the
  bridge process itself is down.
- Protect remote Grafana access with HTTPS, authentication, and a VPN or reverse proxy.
 
The most important software boundary remains the payload contract: the firmware scale, TTN decoder names, MQTT topic, InfluxDB schema, and Grafana query must evolve together.
