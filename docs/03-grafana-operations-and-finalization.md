# 3. Grafana, Operations, Troubleshooting, and Finalization

## 3.1 Add InfluxDB to Grafana

Open http://localhost:3000 and sign in using the Grafana values from `.env`.

Add an InfluxDB data source with:

- Query language: Flux
- URL: `http://influxdb:8086`
- Organization: the value of `INFLUXDB_ORG`, normally `cder_lab`
- Token: the value of `INFLUXDB_TOKEN`
- Default bucket: the value of `INFLUXDB_BUCKET`, normally `telemetry`
- Basic authentication: disabled for this local Compose network

Use `http://influxdb:8086`, not `http://localhost:8086`, because Grafana is inside the Grafana container. Click **Save & Test**. A successful test proves that Grafana can reach InfluxDB and authenticate.

## 3.2 Build the first panel

Create a dashboard panel using the InfluxDB data source. A Flux query for wind speed is:

```flux
from(bucket: "telemetry")
  |> range(start: -1h)
  |> filter(fn: (r) => r["_measurement"] == "wind_telemetry")
  |> filter(fn: (r) => r["_field"] == "wind_speed_ms")
```

The query means:

- `from` selects a bucket.
- `range` limits the time window.
- The measurement filter selects this project's points.
- The field filter selects one value from each point.

Choose a **Time series** panel to see history. Choose a **Gauge** for the latest value. For a wind-speed gauge, set a meaningful range such as 0 to 30 m/s and configure thresholds according to the project's safety or meteorological requirements. Do not treat example thresholds as scientifically authoritative.

Useful panels include:

- Wind speed: time series and current-value gauge.
- Wind direction: time series or a direction-specific visualization, with a 0 to 360 degree range.
- Battery: gauge or stat panel, with a 0 to 100 percent range.
- Device status: table grouped by `device_id`.

## 3.3 Filter by device

Because `device_id` is a tag, it can be filtered efficiently:

```flux
from(bucket: "telemetry")
  |> range(start: -6h)
  |> filter(fn: (r) => r["_measurement"] == "wind_telemetry")
  |> filter(fn: (r) => r["_field"] == "wind_speed_ms")
  |> filter(fn: (r) => r["device_id"] == "stm32-b-l072z-node")
```

For several devices, use a Grafana variable instead of hard-coding a device. This lets an operator choose a device from a dashboard dropdown.

## 3.4 Understand missing values

The bridge writes only fields present in a packet. As a result, a point may contain wind speed but not battery. Grafana should display no value for a missing field, not interpret it as zero.

If a panel is empty, check its time range first. The query above only looks back one hour. Then verify the exact measurement and field names in InfluxDB Data Explorer.

## 3.5 Troubleshooting by symptom

### Docker does not start

Run:

```powershell
docker compose ps
docker compose logs influxdb
docker compose logs grafana
```

Check that Docker Desktop is running and that ports 3000 or 8086 are not already occupied. If a port is occupied, change the host side of the mapping, for example `8087:8086`, and then use the new browser URL. Do not change the container-side URL used by Grafana unless the service itself changed.

### The bridge reports missing environment variables

Confirm that the file is named `.env`, is in the same folder as `ttn_bridge.py`, and contains non-empty values for `TTN_APP_ID`, `TTN_API_KEY`, and `INFLUXDB_TOKEN`. Confirm that the virtual environment has `python-dotenv` installed.

### MQTT authentication fails

Check the regional broker, application ID, API key, and the key's permission to read application traffic. The username must be `<TTN_APP_ID>@ttn`. Keep port `8883` for TLS.

### The bridge connects but no messages arrive

Check that the device is powered, connected to the correct TTN application, and actually transmitting. Confirm the subscribed topic in the log. Check TTN's live application data view. Also confirm that the topic is not accidentally restricted to the wrong device ID.

### Messages arrive but are ignored

Look for `without decoded_payload` or `without known wind fields`. Inspect TTN's decoded payload and compare its names with `wind_speed_ms`, `wind_direction_deg`, and `battery_percent`. Fix the TTN payload formatter or update the bridge contract deliberately; do not silently rename fields in only one layer.

### The bridge logs a write error

Confirm that InfluxDB is running, that `INFLUX_URL` is `http://localhost:8086` when Python runs on the host, and that organization, bucket, and token match the initialized InfluxDB instance. Open http://localhost:8086 to verify the service is reachable.

### Grafana cannot connect to InfluxDB

Use `http://influxdb:8086` from Grafana, not `localhost`. Confirm both containers share the Compose network and that the token has read access to the bucket.

### Data exists but the panel is empty

Check the dashboard time range, bucket, measurement, field, and device filter. Start with the last 24 hours and remove filters one at a time. Confirm that the panel is using Flux and the correct data source.

## 3.6 Backups and data lifecycle

The Compose volumes are persistent storage, not backups. A damaged disk or accidental volume deletion can still lose data. Decide:

- How often to back up InfluxDB data and Grafana dashboards.
- How long raw telemetry should be retained.
- Whether dashboards should be exported or provisioned from files.
- Where backup files are stored and how restoration is tested.

For a pilot, document the Docker volume names and periodically export important Grafana dashboards. For production, define a tested backup and restore procedure before declaring the system complete.

## 3.7 PC to Raspberry Pi migration

The architecture stays the same because the applications are packaged in containers. Copy the project files, install Docker on the Pi, create the Pi's `.env`, and run:

```bash
docker compose up -d
```

The main differences are operational:

- Use ARM-compatible images; the official InfluxDB and Grafana images generally publish multi-architecture variants, but verify the exact versions.
- Use a stable network connection and a fixed hostname or IP if users access the dashboard remotely.
- Protect the Pi and its storage from power loss.
- Do not expose InfluxDB or Grafana directly to the public Internet without authentication, firewall rules, and preferably a reverse proxy with HTTPS.
- Recreate or restore the data volumes intentionally; copying only YAML and Python files does not copy historical data.

## 3.8 Definition of done

Consider the project ready for handover when all of these are true:

- A fresh operator can follow the setup guide without guessing variable names.
- `.env.example` contains names but no real secrets.
- Docker containers restart successfully and data survives a normal restart.
- The bridge connects with TLS and logs its subscription.
- At least one real uplink is stored in InfluxDB.
- Grafana shows wind speed, direction, and battery with a documented time range.
- Missing fields and malformed packets do not stop the bridge.
- Credentials and tokens have been rotated from examples.
- Backup, restore, retention, and Raspberry Pi procedures are documented.
- The payload field contract is recorded and agreed with the firmware owner.

## 3.9 A sensible next development stage

After the first end-to-end demo works, improve one concern at a time:

1. Add automated tests for payload parsing and numeric validation.
2. Add reconnect and retry metrics.
3. Add MQTT and InfluxDB health checks.
4. Move long-running execution to a service manager or a dedicated container.
5. Add dashboard provisioning so Grafana can be recreated consistently.
6. Add retention policies and backups.
7. Add secure remote access for authorized users.

The most important habit is to keep the boundaries visible: device format, TTN decoded payload, MQTT topic, InfluxDB schema, and Grafana query are separate contracts. Test each one before adding more complexity.