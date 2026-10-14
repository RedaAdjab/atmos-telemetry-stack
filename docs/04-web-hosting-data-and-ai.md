# 4. Web Hosting, Data Storage, Monitoring, and AI

This document answers an important question: what does it mean for this project to "run on the web"?

## 4.1 What is running right now?

Your current system is running locally on your Windows PC:

```text
STM32 node
  -> LoRaWAN gateway
  -> TTN cloud
  -> MQTT over the Internet
  -> ttn_bridge.py on your PC
  -> InfluxDB Docker container on your PC
  -> Grafana Docker container on your PC
  -> browser on your PC
```

The two URLs are local URLs:

- `http://localhost:8086` opens InfluxDB on your PC.
- `http://localhost:3000` opens Grafana on your PC.

`localhost` means the current computer. Someone in another building cannot open your `localhost:3000`. At the moment, this is a local development and laboratory deployment, not a public website.

The current Docker status should look conceptually like this:

```text
lora-influxdb:  host port 8086 -> container port 8086
lora-grafana:  host port 3000 -> container port 3000
Python bridge: running directly in your PowerShell terminal
```

The bridge is not inside Docker in the current project. It runs as a normal Python process using the `.venv` environment. This is useful while learning because its logs are visible immediately.

## 4.2 What happens when a measurement is sent?

Assume the STM32 sends one measurement every 10 seconds.

1. The STM32 measures wind speed, direction, and battery level.
2. It encodes those values into a LoRaWAN uplink.
3. The gateway receives the radio packet and forwards it to TTN.
4. TTN authenticates and decodes the packet for the application.
5. TTN publishes a JSON event to its MQTT broker.
6. `ttn_bridge.py` is subscribed to the application's `.../devices/+/up` topic.
7. Paho MQTT calls the bridge's `on_message()` function.
8. The bridge extracts `device_id` and `decoded_payload`.
9. The bridge creates an InfluxDB point.
10. The InfluxDB client sends that point over HTTP to `http://localhost:8086`.
11. InfluxDB stores the point in the `telemetry` bucket.
12. Grafana periodically queries InfluxDB and redraws its panels.

The Python bridge is therefore an adapter. TTN speaks MQTT and sends JSON; InfluxDB expects time-series writes. The bridge translates between those two interfaces and applies validation.

## 4.3 How data is stored

The bridge creates data with this logical schema:

```text
measurement: wind_telemetry
tag:         device_id
fields:      wind_speed_ms
             wind_direction_deg
             battery_percent
time:        write timestamp assigned by InfluxDB
```

A conceptual record might be:

```text
time: 2026-09-22T14:00:00Z
device_id: stm32-b-l072z-node
wind_speed_ms: 5.2
wind_direction_deg: 180.5
battery_percent: 100
```

InfluxDB physically stores its files inside the container at `/var/lib/influxdb2`. The Compose file maps that directory to the named volume `influxdb_data`. Docker gives the project a concrete volume name such as `deployment_influxdb_data`.

This means:

- Restarting a container normally does not erase data.
- `docker compose down` removes containers but keeps named volumes.
- `docker compose down -v` removes the volumes and can erase the database.
- The Docker volume is persistence, not a backup. A disk failure or accidental deletion can still lose data.

Grafana stores dashboards, users, data-source settings, and other application state in its own volume, `grafana_data`. InfluxDB data and Grafana configuration are separate; backing up one does not back up the other.

## 4.4 What does "hosted on the web" mean?

There are three different meanings that are often confused.

### Local web application

This is your current setup. Grafana and InfluxDB use HTTP web interfaces, but they are reachable only from the local PC through `localhost`.

### LAN-hosted application

You could make Grafana reachable by other devices on the same laboratory network using the PC's private IP address, for example:

```text
http://192.168.1.50:3000
```

This requires firewall rules and a stable PC address. It still does not make the dashboard public on the Internet. Never expose InfluxDB's administrative port casually; users should normally receive Grafana access, not direct database administration access.

### Internet-hosted application

For access from CDER or another location, the services need to run on an always-on machine reachable from the Internet or through a private network. Common choices are:

- A Raspberry Pi at the site, reachable through a VPN.
- A virtual private server or cloud VM.
- Managed cloud services for InfluxDB and Grafana.
- A private institutional server or Kubernetes platform.

The application should normally be reached through a domain and HTTPS:

```text
https://dashboard.example.org
```

A reverse proxy receives HTTPS traffic, handles certificates, and forwards authorized requests to Grafana. Authentication, firewall rules, VPN access, and backups are part of hosting. Port-forwarding `3000` directly from a home router is not a sufficient production security design.

## 4.5 What changes when moving to a Raspberry Pi or server?

The logical architecture remains the same:

```text
TTN MQTT -> Python bridge -> InfluxDB -> Grafana -> browser
```

The physical location changes. On the remote machine:

- Docker runs the InfluxDB and Grafana containers.
- The Python bridge runs there as a service instead of in an open PowerShell window.
- `INFLUX_URL` becomes `http://localhost:8086` if the bridge runs directly on that same machine.
- Grafana still uses `http://influxdb:8086` because it reaches InfluxDB through the Docker network.
- Users access Grafana through the machine's private address, VPN address, or public HTTPS domain.

For a reliable deployment, run the bridge with a service manager such as `systemd` on Linux, or put the bridge into its own Docker service with restart policies and health checks. A terminal process is appropriate for a lab but is fragile for unattended operation.

## 4.6 How monitoring uses the data

Monitoring means continuously observing the system and notifying people when a condition matters. It has two layers.

### Measurement monitoring

These rules concern the environment:

- Wind speed exceeds a safety limit.
- Battery percentage falls below a maintenance limit.
- Wind direction remains in an unusual sector.
- A sensor value is outside its physical range.

Grafana can visualize these conditions with thresholds, colors, and alert rules. A Grafana alert may evaluate a query every minute and send email, Teams, Slack, or a webhook notification.

### System-health monitoring

These rules concern the pipeline itself:

- No uplink has arrived from a device for 5 minutes.
- MQTT disconnected.
- The Python bridge is not running.
- InfluxDB is unavailable.
- Writes are failing.
- A device is sending malformed or incomplete payloads.

This project currently logs failures but does not yet store bridge-health metrics. A production version should expose or write counters such as `mqtt_connected`, `messages_received`, `messages_rejected`, `influx_writes_failed`, and `last_message_timestamp`.

An important distinction: a flat wind-speed line may mean calm weather, but no data at all may mean a dead battery or broken network. Dashboards should make those states visually different.

## 4.7 How applications use the data

InfluxDB can be queried in several ways:

- Grafana Flux queries for dashboards and alerts.
- The InfluxDB Python client for analysis scripts.
- The InfluxDB HTTP API for other applications.
- Scheduled exports to CSV or Parquet for offline analysis.

A separate web application could call a backend API that queries InfluxDB. The browser should not normally receive an unrestricted InfluxDB admin token. A backend can authenticate users, limit queries, and return only the data each user is allowed to see.

Example application uses include:

- A live site-status page.
- Daily and monthly wind summaries.
- Device maintenance reports.
- Research exports.
- Threshold-based notifications.
- Forecast and anomaly-detection services.

## 4.8 How AI or machine learning would use this data

AI is not inserted directly between MQTT and Grafana by default. A sensible architecture adds a separate analysis path:

```text
InfluxDB historical data
  -> extraction job
  -> cleaning and feature engineering
  -> training dataset
  -> trained model
  -> prediction service
  -> predictions written back to InfluxDB
  -> Grafana prediction panel and alerts
```

### Step 1: Collect enough trustworthy history

The model needs many correctly timestamped examples. Record the raw measurements and useful metadata such as device ID, firmware version, sensor status, and location. Ten minutes of data is not a meaningful training set for most predictions.

### Step 2: Define the prediction target

Possible targets are different problems:

- Forecast wind speed 10 minutes ahead.
- Predict whether wind speed will exceed a threshold.
- Detect abnormal sensor behavior.
- Estimate battery depletion time.
- Classify a likely sensor or communication fault.

The target determines the model, labels, features, and evaluation metric. "Use AI on the data" is not specific enough until the target and time horizon are defined.

### Step 3: Extract and prepare data

An analysis job queries InfluxDB and converts records into a table. It may create features such as:

- Current wind speed and direction.
- Rolling mean over 1, 5, and 15 minutes.
- Rolling maximum and standard deviation.
- Direction represented as sine and cosine, because 359 degrees and 1 degree are close, not far apart.
- Battery trend over the last hour.
- Time of day, day of week, and season.
- Time since the last valid uplink.

Cleaning must handle missing values, duplicate points, sensor resets, impossible ranges, and irregular sampling intervals.

### Step 4: Split data correctly

Time-series data should normally be split chronologically. Train on earlier dates and test on later dates. Randomly mixing future rows into the training set can create data leakage and produce an unrealistically optimistic result.

### Step 5: Train and evaluate

Start with a simple baseline, such as "the next wind speed equals the current wind speed." Compare a model against that baseline. Useful metrics depend on the target:

- Forecasting: MAE or RMSE.
- Classification: precision, recall, F1, and confusion matrix.
- Anomaly detection: false alarms and missed anomalies.

Keep the trained model versioned with its feature definitions and training time. A model without its preprocessing rules cannot be reproduced reliably.

### Step 6: Serve predictions

A prediction service can run every few minutes, query recent InfluxDB values, calculate features, and write results back as a separate measurement:

```text
measurement: wind_predictions
tags:        device_id, model_version
fields:      predicted_wind_speed_ms
             prediction_lower_bound
             prediction_upper_bound
             anomaly_score
time:        prediction creation time
```

Keeping predictions separate from raw observations makes it possible to compare the prediction with the later actual measurement. Grafana can show both on one panel.

### Step 7: Operate and monitor the model

A deployed model can become inaccurate when seasons, hardware, location, or sensor behavior changes. Track:

- Prediction error over time.
- Missing input rate.
- Model version.
- Data distribution changes.
- Alert frequency and false positives.

Retraining should be a deliberate, tested process rather than an automatic action with no review.

## 4.9 A practical future architecture

For a small research or laboratory system:

```text
Docker Compose host
  - InfluxDB
  - Grafana
  - Python bridge service
  - optional prediction worker
```

For a larger deployment:

```text
TTN MQTT
  -> resilient ingestion service
  -> InfluxDB or managed time-series database
  -> Grafana
  -> alerting/observability system
  -> batch feature pipeline
  -> model registry and prediction API
```

Do not add the larger architecture before the smaller one is reliable. First prove that measurements arrive, are stored correctly, survive restarts, and can be queried. Then add alerts, health metrics, backups, and finally prediction workloads.

## 4.10 Security boundaries

There are several separate secrets and trust boundaries:

- TTN API key: permits MQTT access to application traffic.
- InfluxDB token: permits database API operations.
- Grafana admin password: controls dashboard administration.
- Network exposure: determines who can reach Grafana or InfluxDB.

Use least privilege where possible. The bridge needs write access to the telemetry bucket; Grafana normally needs read access. Do not use an all-powerful admin token for every service in production. Rotate credentials, use HTTPS for remote access, restrict firewall ports, and keep database ports private whenever possible.

## 4.11 What the current project still needs before production hosting

The current lab proves the core data path, but production hosting would normally add:

- A service definition or container for the Python bridge.
- Automatic restart and reconnect behavior.
- Health checks and bridge metrics.
- A backup and restore procedure for both Docker volumes.
- Retention policy and storage sizing.
- HTTPS, authentication, firewall rules, and preferably VPN access.
- Centralized logs and time synchronization.
- Tests for payload parsing and InfluxDB writes.
- A documented firmware/TTN payload schema.
- A defined AI target, dataset, baseline, and evaluation procedure.

The key idea is that Grafana is the presentation layer, not the whole platform. The platform is the complete chain: reliable sensing, transport, ingestion, storage, querying, visualization, alerting, and eventually analysis or prediction.