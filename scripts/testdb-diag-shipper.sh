#!/usr/bin/env bash
# Startet den db2diag-Shipper im Db2-Test-Container (compose.testdb.yaml).
# Der Shipper laeuft im Container als db2inst1 und schickt an Loki im selben Compose-Netz.
# Nach einem Neustart des Containers erneut ausfuehren.
set -euo pipefail
RUNTIME="${CONTAINER_RUNTIME:-podman}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! "$RUNTIME" exec db2-test bash -c 'command -v python3 >/dev/null'; then
  echo "python3 fehlt im Container db2-test - Shipper kann dort nicht laufen." >&2
  exit 1
fi

"$RUNTIME" cp "$DIR/diag-shipper/db2diag_shipper.py" db2-test:/tmp/db2diag_shipper.py
"$RUNTIME" exec db2-test pkill -f db2diag_shipper.py 2>/dev/null || true
"$RUNTIME" exec -d -u db2inst1 \
  -e LOKI_URL=http://loki:3100 -e HOST_LABEL=db2-test -e EXTRA_LABELS=env=test \
  db2-test bash -lc 'exec python3 /tmp/db2diag_shipper.py >> /tmp/db2diag_shipper.log 2>&1'
sleep 3
echo "Shipper gestartet. Log:"
"$RUNTIME" exec db2-test tail -5 /tmp/db2diag_shipper.log
