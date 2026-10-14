#!/usr/bin/env bash
set -euo pipefail

backup_root="${BACKUP_DIR:-./backups}"
mkdir -p "$backup_root"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="/backups/$stamp"

docker compose exec -T influxdb influx backup "$target"

find "$backup_root" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' |
  sort -nr |
  tail -n +8 |
  cut -d' ' -f2- |
  xargs -r rm -rf

echo "InfluxDB backup written to $backup_root/$stamp"
