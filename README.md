# ATMOS Wind Telemetry

This project receives decoded uplinks from TTN and stores them in InfluxDB for Grafana.

```text
Nucleo-L073RZ + SX1272 -> Laird gateway -> TTN -> MQTT -> Python bridge -> InfluxDB -> Grafana
```

The bridge expects TTN `decoded_payload` to contain these numeric fields:

```json
{
  "wind_speed": 5.2,
  "wind_direction": 180.5,
  "gust": 7.1,
  "temperature": 24.3
}
```

These names match the decoder in the project request. They are intentionally different from the older reference documents.

## First setup on Windows

1. Copy the environment template:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Open `.env` and replace every placeholder. Use the exact TTN application ID and API key. The API key needs permission to read application uplink traffic.

3. Start the stack from this folder:

   ```powershell
   docker compose up -d
   docker compose ps
   ```

4. Open:

   - Grafana: http://localhost:3000
   - InfluxDB: http://localhost:8086

   Grafana uses `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from `.env`. The InfluxDB data source is provisioned automatically; dashboards created in the Grafana UI are stored in Grafana's persistent Docker volume.

5. Watch the bridge:

   ```powershell
   docker compose logs -f bridge
   ```

   A working connection logs a subscription to `v3/<application-id>@ttn/devices/+/up`, followed by `Stored telemetry from ...` after an uplink arrives.

## Local Python virtual environment

The Docker stack already contains an isolated Python environment for the bridge. A Windows virtual environment is still useful for editing, syntax checks, and running the bridge directly without Docker.

Create it once from this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

With the environment active, validate the bridge with:

```powershell
python -m py_compile .\ttn_bridge.py
```

To run the bridge directly, set `INFLUX_URL=http://localhost:8086` in `.env`, start only InfluxDB and Grafana with Docker, and run:

```powershell
python .\ttn_bridge.py
```

Do not run the direct Python bridge and the Compose `bridge` service at the same time, or both processes will consume the same MQTT uplinks.

Leave the environment with `deactivate` when finished. The `.venv` folder is excluded from Git.

## Temporary local gateway packet capture

When the router has no Internet access, TTN cannot receive packets from the
gateway. The standalone [`gateway_packet_capture.py`](./gateway_packet_capture.py)
script can show Semtech UDP packet-forwarder traffic on this laptop without
using MQTT, TTN, Docker, or any third-party Python package.

First find the laptop's LAN address with `ipconfig`, then configure the
gateway's packet-forwarder server address to that address and UDP port `1700`.
Allow Python through the Windows Firewall if prompted. From this folder, run:

```powershell
python .\gateway_packet_capture.py --output .\gateway-packets.jsonl
```

The script prints one line for each received radio uplink and writes the
complete JSON payload to the optional JSONL file. Use `Ctrl+C` to stop it.
`--count 1` is useful for a short capture:

```powershell
python .\gateway_packet_capture.py --count 1
```

This is a receiver, not a packet sniffer: packets sent to the original TTN
address cannot be observed by the laptop unless the gateway is configured to
send them here (or the network has a packet mirror). Restore the gateway's TTN
server address after troubleshooting. The script acknowledges received gateway
packets by default so the gateway does not repeatedly retry them; use
`--no-ack` only when that behavior is not desired.

## Important settings

- Use `TTN_PORT=8883` for TLS. The bridge verifies the broker certificate on this port.
- The bridge connects to InfluxDB at `http://influxdb:8086` because both run in Docker.
- Grafana also uses `http://influxdb:8086` through the Docker network.
- The browser uses `localhost` because the ports are published on Windows.
- The first InfluxDB initialization uses the values in `.env`. Existing Docker volumes keep their original credentials after that; changing `.env` later does not reinitialize an existing database.
- Grafana dashboards created or edited in the UI are kept in the `grafana_data` Docker volume.

## Useful commands

```powershell
# Show service status
 docker compose ps

# Follow all service logs
 docker compose logs -f

# Stop containers while preserving data
 docker compose down

# Stop containers and delete all database/dashboard data
 docker compose down -v
```

Do not use `docker compose down -v` unless deleting the local data is intentional.

To back up a dashboard, open it in Grafana, choose **Dashboard settings** or **Share**, and use **Export** to save its JSON. This is an additional backup; normal `docker compose down` preserves the dashboard automatically.

## Troubleshooting order

1. Check that the bridge logs a successful MQTT connection and subscription.
2. Check TTN Live Data for an uplink and its decoded payload.
3. Check the bridge log for `Stored telemetry from ...`.
4. Check InfluxDB Data Explorer for measurement `wind_telemetry`.
5. Check Grafana's time range; the dashboard defaults to the last hour.

If the bridge says it is connected but ignores packets, compare the TTN decoded payload names with the four names listed above. If Docker reports a port conflict, change only the host side of `3000:3000` or `8086:8086` and use the new browser port.
