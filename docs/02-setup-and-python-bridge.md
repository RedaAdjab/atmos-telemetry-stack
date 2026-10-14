# 2. Setup and Python Bridge Walkthrough

This guide explains the setup for a Windows development computer. The same architecture can later run on a Raspberry Pi, provided Docker and Python are available for its operating system and CPU architecture.

## 2.1 Prerequisites

Install:

- Docker Desktop, with Docker Compose available as `docker compose`.
- Python 3.10 or newer.
- A TTN application with at least one registered device and an uplink payload formatter.
- Permission to create a TTN API key.

Check the tools:

```powershell
docker --version
docker compose version
python --version
```

PowerShell is used in the commands below. On Linux or macOS, activate the virtual environment with `source .venv/bin/activate` instead.

## 2.2 Configure secrets safely

The repository includes `.env.example`, which documents the required variable names. Copy it to `.env`:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace every placeholder:

```dotenv
INFLUXDB_USERNAME=admin
INFLUXDB_PASSWORD=use-a-long-local-password
INFLUXDB_ORG=cder_lab
INFLUXDB_BUCKET=telemetry
INFLUXDB_TOKEN=use-a-long-influx-token
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=use-a-long-grafana-password
TTN_BROKER=eu1.cloud.thethings.network
TTN_PORT=8883
TTN_APP_ID=your-actual-application-id
TTN_API_KEY=your-actual-ttn-api-key
```

The Docker Compose file reads the InfluxDB and Grafana values from `.env`. `ttn_bridge.py` uses `python-dotenv` to load the same file into environment variables.

Never put real credentials in `.env.example`, Markdown files, source code, screenshots, or shell history. The `.gitignore` file already excludes `.env` and Python cache files.

## 2.3 Create the Python environment

A virtual environment keeps this project's packages separate from other Python projects:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

The packages are:

- `paho-mqtt`: MQTT connection, authentication, subscription, and callbacks.
- `influxdb-client`: InfluxDB connection and point-writing API.
- `python-dotenv`: loads `.env` values for local development.

## 2.4 Start InfluxDB and Grafana

From the project folder:

```powershell
docker compose up -d
docker compose ps
```

The `-d` means detached mode: Docker starts the containers and returns control of the terminal.

Open:

- InfluxDB: http://localhost:8086
- Grafana: http://localhost:3000

The initial InfluxDB setup is created from the `DOCKER_INFLUXDB_INIT_*` variables on the first run. The named Docker volumes preserve data when containers are recreated. Changing initialization variables later does not automatically change an already-initialized database.

Useful lifecycle commands:

```powershell
docker compose logs -f influxdb
docker compose logs -f grafana
docker compose restart
docker compose down
```

Do not use `docker compose down -v` unless you intentionally want to delete the named volumes and all stored InfluxDB/Grafana data.

## 2.5 The repeatable start-to-finish routine

The following is the normal routine after the first-time setup is complete. It assumes the project is located at `D:\ESI\SPE\Deployment`.

### Start

1. Start Docker Desktop. Wait until Docker Desktop says that the engine is running.
2. Open PowerShell and run:

```powershell
Set-Location D:\ESI\SPE\Deployment
.\.venv\Scripts\Activate.ps1
docker compose up -d
docker compose ps
python ttn_bridge.py
```

The commands do the following:

- `Set-Location` moves PowerShell into the folder containing the Compose file and Python script.
- The activation command selects this project's installed Python packages.
- `docker compose up -d` starts InfluxDB and Grafana in the background.
- `docker compose ps` shows whether the two containers are running.
- `python ttn_bridge.py` starts the MQTT-to-InfluxDB bridge in the foreground.

Keep the final terminal open. It is the live ingestion process. `Ctrl+C` stops the bridge.

Open these addresses in a browser while the project is running:

- Grafana: http://localhost:3000
- InfluxDB: http://localhost:8086

### Confirm that it is working

You should see all of the following:

1. `docker compose ps` reports `Up` for `lora-influxdb` and `lora-grafana`.
2. The bridge logs a message saying it connected to TTN and subscribed to a `v3/.../devices/+/up` topic.
3. The STM32 sends an uplink.
4. The bridge logs `Stored telemetry from ...`.
5. Grafana displays the new point after its dashboard time range includes the current time.

If step 1 fails, inspect Docker. If step 2 fails, inspect TTN credentials or network access. If step 2 succeeds but step 4 does not happen, inspect the device uplink and decoded payload. If step 4 succeeds but Grafana is empty, inspect the data source, query, and time range.

### Stop

Stop the foreground bridge with `Ctrl+C`, then optionally stop the containers:

```powershell
docker compose down
deactivate
```

Stopping the containers is optional if you want the database and Grafana available between sessions. If you stop them, normal data remains in the named Docker volumes. Do not add `-v` unless you intentionally want to delete those volumes and their data.

### If the terminal is closed unexpectedly

Simply repeat the start commands. Existing containers and named volumes are reused. If the bridge was already running in another terminal, do not start a second bridge unless you deliberately want two consumers writing the same data.

### If Docker reports that its engine is unavailable

Start Docker Desktop first. The error mentioning `dockerDesktopLinuxEngine` means the Docker command-line client cannot reach the Docker Desktop engine; it is not a Python or TTN error. Once Docker Desktop is running, retry `docker compose up -d`.

## 2.6 Create TTN MQTT credentials

In TTN Console, open the application, then its MQTT integration/credentials area. Record:

- The regional broker hostname, such as `eu1.cloud.thethings.network`.
- The application ID.
- An API key with permission to read application traffic.

Use the application ID in `TTN_APP_ID`, not the display name of a device. Use the complete API key in `TTN_API_KEY`. The bridge transforms the application ID into the MQTT username `<application-id>@ttn`.

Use TLS port `8883`. The bridge enables certificate verification whenever `TTN_PORT` is `8883`.

## 2.7 Run the bridge

With the virtual environment active and the containers running:

```powershell
python ttn_bridge.py
```

A successful startup should log a connection attempt, followed by a subscription message similar to:

```text
Connected to TTN and subscribed to v3/your-application-id@ttn/devices/+/up
```

Leave this process running while the device sends uplinks. Stop it with `Ctrl+C`.

## 2.8 How `ttn_bridge.py` works

### Configuration loading

`load_dotenv()` reads `.env`. `os.getenv()` then supplies defaults and configured values. Required secrets are checked by `required_settings()` before any network client is created.

This fails early with a useful error instead of silently connecting with an empty password. The required values are `TTN_APP_ID`, `TTN_API_KEY`, and `INFLUXDB_TOKEN`.

### Logging

The `logging` setup gives each message a timestamp and severity. Set `LOG_LEVEL=DEBUG` while investigating a local issue, but avoid logging secrets or full sensitive payloads in production.

### MQTT connection

The code creates a Paho client using `CallbackAPIVersion.VERSION2`. It sets the MQTT username and API key, enables TLS for port `8883`, registers `on_connect` and `on_message`, and calls `loop_forever()`.

`loop_forever()` is important: MQTT needs a network loop to receive messages, handle keepalive traffic, and invoke callbacks. Without it, the program would connect and then stop processing network events.

### Subscription

`on_connect()` subscribes only after the broker accepts the connection. This is important because a subscription made before a successful connection is not a reliable startup strategy.

The topic contains `devices/+/up`, so every device in the selected TTN application is accepted. To restrict ingestion to one device, replace `+` with its device ID.

### JSON extraction

`on_message()` decodes the MQTT bytes as UTF-8 JSON, then reads:

```text
end_device_ids.device_id
uplink_message.decoded_payload
```

Messages without a decoded payload are ignored. This is normal when a payload formatter has not run or when an uplink does not contain application data.

### Numeric validation

`as_number()` converts values to `float` and rejects non-finite values such as `NaN` and infinity. This prevents invalid numeric values from entering the database.

The bridge checks each known field independently. A message containing only battery data is still stored. This is safer than inserting artificial zeroes for missing measurements, because zero wind is different from an absent wind sensor value.

### InfluxDB point creation

The code creates one point with measurement `wind_telemetry`, adds `device_id` as a tag, and adds whichever known values exist as fields. It uses the synchronous write API, so the callback waits for InfluxDB to accept the point before logging success.

Synchronous writing is easy to understand and appropriate for a small lab deployment. A higher-volume deployment may use batching, retries, and a queue so a slow database does not block MQTT message handling.

### Error handling

Expected malformed-message errors are logged as processing errors. Unexpected write or library errors are logged with a traceback using `logger.exception()`. The bridge continues running instead of terminating because one bad packet should not stop the whole ingestion service.

## 2.9 A first diagnostic test

Before debugging Grafana, prove each boundary in order:

1. A device sends an uplink in TTN Console.
2. The bridge logs a successful TTN connection.
3. The bridge logs `Stored telemetry from ...`.
4. InfluxDB's data explorer can find the measurement.
5. Grafana can query the same bucket.

This order avoids changing dashboard queries when the real problem is an MQTT credential or a missing decoded payload.