#!/usr/bin/env bash
# Erzeugt etwas Last auf der Test-Datenbank, damit die Dashboards Daten zeigen.
# Aufruf: ./scripts/workload.sh [Runden]   (Standard: 50)
# Anderer Container/DB: DB2_CONTAINER=sc-db2 DB2_TEST_DBNAME=TESTDB ./scripts/workload.sh
set -euo pipefail
ROUNDS="${1:-50}"
RUNTIME="${CONTAINER_RUNTIME:-podman}"
CONTAINER="${DB2_CONTAINER:-db2-test}"
DB="${DB2_TEST_DBNAME:-TESTDB}"

"$RUNTIME" exec -i -u db2inst1 "$CONTAINER" bash -l -s <<SQL
db2 connect to $DB > /dev/null
db2 -x "SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA='DEMO' AND TABNAME='ORDERS'" | grep -q 1 || {
  db2 "CREATE TABLE DEMO.ORDERS (ID INTEGER NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
       CUSTOMER INTEGER, AMOUNT DECIMAL(10,2), CREATED TIMESTAMP DEFAULT CURRENT TIMESTAMP)"
  db2 "CREATE INDEX DEMO.IX_ORDERS_CUST ON DEMO.ORDERS (CUSTOMER)"
}
for i in \$(seq 1 $ROUNDS); do
  db2 +c "INSERT INTO DEMO.ORDERS (CUSTOMER, AMOUNT)
          SELECT INT(RAND()*1000), DECIMAL(RAND()*500, 10, 2) FROM SYSCAT.COLUMNS FETCH FIRST 500 ROWS ONLY" > /dev/null
  db2 commit > /dev/null
  db2 "SELECT CUSTOMER, SUM(AMOUNT) FROM DEMO.ORDERS GROUP BY CUSTOMER ORDER BY 2 DESC FETCH FIRST 10 ROWS ONLY" > /dev/null
  db2 "SELECT COUNT(*) FROM DEMO.ORDERS WHERE AMOUNT > 250" > /dev/null
  echo "Runde \$i/$ROUNDS"
done
db2 terminate > /dev/null
SQL
