#!/usr/bin/env bash
set -euo pipefail

backup="${1:-}"
if [[ -z "$backup" || ! -d "$backup" ]]; then
  echo "Usage: $0 <backup-directory>" >&2
  exit 2
fi

source .env
before="$(
  docker compose exec -T influxdb influx query \
    --org "$INFLUXDB_ORG" \
    --token "$INFLUXDB_TOKEN" \
    'from(bucket: "'$INFLUXDB_BUCKET'") |> range(start: 0) |> filter(fn: (r) => r._measurement == "wind_telemetry") |> count()' |
    awk 'NF && $1 ~ /^[0-9]+$/ { total += $1 } END { print total + 0 }'
)"

temp_volume="atmos-restore-test-$RANDOM"
docker volume create "$temp_volume" >/dev/null
cleanup() {
  docker stop "$temp_volume" >/dev/null 2>&1 || true
  docker volume rm "$temp_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

backup_path="$(cd "$(dirname "$backup")" && pwd)/$(basename "$backup")"
docker run -d --rm --name "$temp_volume" \
  -v "$temp_volume:/var/lib/influxdb2" \
  -v "$backup_path:/backup:ro" \
  influxdb:2.7 >/dev/null
until docker exec "$temp_volume" influx ping >/dev/null 2>&1; do sleep 1; done
docker exec "$temp_volume" influx restore --full "/backup"
after="$(
  docker exec "$temp_volume" influx query \
    --org "$INFLUXDB_ORG" \
    --token "$INFLUXDB_TOKEN" \
    'from(bucket: "'$INFLUXDB_BUCKET'") |> range(start: 0) |> filter(fn: (r) => r._measurement == "wind_telemetry") |> count()' |
    awk 'NF && $1 ~ /^[0-9]+$/ { total += $1 } END { print total + 0 }'
)"

if [[ "$before" != "$after" ]]; then
  echo "Restore verification failed: source=$before restored=$after" >&2
  exit 1
fi
echo "Restore verification passed: $after wind telemetry field values"
