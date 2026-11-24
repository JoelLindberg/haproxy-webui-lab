#!/usr/bin/env bash
set -euo pipefail

: "${DATAPLANE_PASSWORD:?DATAPLANE_PASSWORD must be set}"

TEMPLATE="/usr/local/etc/haproxy/dataplaneapi.yml.template"
OUT="/usr/local/etc/haproxy/dataplaneapi.yml"

# Render template with environment variable
envsubst < "$TEMPLATE" > "$OUT"

# Start the original base image entrypoint
exec /start.sh "$@"