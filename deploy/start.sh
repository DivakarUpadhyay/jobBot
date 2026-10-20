#!/bin/sh
# Start command for one-service hosts (Railway, Render): customer portal, admin panel and scheduler in one
# process on $PORT, data on the volume at $JOBBOT_DATA. On a fresh volume with JOBBOT_SEED_TOKEN set, it first
# waits for the one-time data upload from tools/push_seed.py.
set -e
DATA="${JOBBOT_DATA:-/data}"
mkdir -p "$DATA"
if [ ! -f "$DATA/jobbot.db" ] && [ -n "$JOBBOT_SEED_TOKEN" ]; then
  python deploy/seed_receiver.py
fi
exec python -m jobbot serve --host 0.0.0.0 --site all --scheduler --port "${PORT:-8765}"
