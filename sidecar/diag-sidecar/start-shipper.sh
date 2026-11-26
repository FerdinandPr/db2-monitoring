#!/bin/bash
# Startet den db2diag-Shipper im Sidecar-Container als Instanzbesitzer.
# 1. wartet, bis der Db2-Container die Instanz im geteilten Volume angelegt hat
# 2. legt den Instanzbesitzer mit derselben UID/GID wie im Volume an
# 3. laedt db2profile und startet db2diag_shipper.py ("db2diag -f -json")
set -euo pipefail

INST="${DB2INSTANCE:-db2inst1}"
INST_HOME="${DB2_INSTANCE_HOME:-/database/config/$INST}"
PROFILE="$INST_HOME/sqllib/db2profile"

echo "db2diag-sidecar: warte auf $PROFILE (Db2-Setup im Datenbank-Container) ..."
until [ -r "$PROFILE" ]; do sleep 10; done

uid="$(stat -c %u "$INST_HOME/sqllib")"
gid="$(stat -c %g "$INST_HOME/sqllib")"
getent group "$gid" >/dev/null || groupadd -g "$gid" db2iadm1
if [ "$(id -u "$INST" 2>/dev/null || true)" != "$uid" ]; then
  # userdel ohne -r: das Home im Volume bleibt unberuehrt
  id "$INST" >/dev/null 2>&1 && userdel "$INST"
  useradd -M -N -o -u "$uid" -g "$gid" -d "$INST_HOME" -s /bin/bash "$INST"
fi
echo "db2diag-sidecar: Instanzbesitzer $INST (uid=$uid, gid=$gid), Home $INST_HOME"

export HOME="$INST_HOME" USER="$INST" LOGNAME="$INST" DB2INSTANCE="$INST"
export DB2DIAG_CMD="${DB2DIAG_CMD:-db2diag -f -json}"
exec runuser -u "$INST" -- bash -c ". '$PROFILE' && exec python3 -u /opt/db2diag-shipper/db2diag_shipper.py"
