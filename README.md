# Db2 LUW Monitoring mit Prometheus und Grafana

Container-basierte Monitoring-Umgebung für Db2 LUW (11.1 oder neuer):

```
Db2  ──SQL (MON_GET_*)──▶  db2-exporter  ──/metrics──▶  Prometheus ──┐
                           (Python, ibm_db)              (+ Alerts)     ├──▶  Grafana
db2diag -f -json ────────▶ db2diag-shipper ──push──────▶  Loki ───────┘     (3 Dashboards)
                           (auf dem Db2-Server)          (+ Alerts)
```

- **db2-exporter**: kleiner Python-Exporter. Er führt die SQL-Abfragen aus `config/exporter/queries.yaml` bei jedem Scrape aus. Mehrere Datenbanken sind möglich.
- **Prometheus**: speichert die Metriken (Standard: 30 Tage) und wertet die Alert-Regeln aus.
- **Loki** + **db2diag-shipper**: Der Shipper läuft auf dem Db2-Server, liest `db2diag -f -json` mit und schickt jeden Eintrag an Loki.
- **Grafana**: Datasources und Dashboards werden beim Start automatisch eingerichtet (Provisioning).
- **db2-test** (optional): Db2 Community Edition zum Ausprobieren.

## Schnellstart

Voraussetzung: Podman (mit `podman compose` oder `podman-compose`) auf x86_64. Docker Compose funktioniert genauso.

```bash
cp .env.example .env        # Zugangsdaten eintragen
```

### Variante 1: Bestehende Db2-Datenbank überwachen

1. Monitoring-Benutzer anlegen und berechtigen (siehe [Berechtigungen](#berechtigungen)).
2. In `.env` die Variablen `DB2_HOST`, `DB2_PORT`, `DB2_DATABASE`, `DB2_USER` und `DB2_PASSWORD` setzen.
3. Starten:

```bash
podman compose up -d --build
```

### Variante 2: Mit Test-Datenbank im Container

```bash
podman compose -f compose.yaml -f compose.testdb.yaml up -d --build
podman logs -f db2-test          # warten bis "Setup has completed" erscheint (einige Minuten)
./scripts/workload.sh 100        # etwas Last erzeugen, damit die Graphen Daten zeigen
```

Der Db2-Community-Container braucht `privileged`. Bei Podman unter Windows/macOS sollte die Podman-Machine deshalb **rootful** laufen:
`podman machine set --rootful` (danach `podman machine stop` und `podman machine start`).

Die Test-Datenbank wird als Instanzbesitzer `db2inst1` abgefragt. Die externe Datenbank lässt sich zusätzlich überwachen: dazu den auskommentierten Block in `config/exporter/targets.testdb.yaml` aktivieren.

### Variante 3: Sidecar-Stack (Db2 + db2diag-Sidecar + Monitor)

Eigenständiges Compose-Projekt im Ordner [`sidecar/`](sidecar/README.md): Db2-Container, ein Sidecar, der `db2diag.log` aus dem geteilten Volume an Loki schickt, und der komplette Monitor. Alles ist vorkonfiguriert.

```bash
cd sidecar && podman compose up -d --build
```

### Aufrufen

| Dienst     | URL                              | Hinweis                         |
|------------|----------------------------------|---------------------------------|
| Grafana    | http://localhost:3000            | Login aus `.env` (Standard admin/admin) |
| Prometheus | http://localhost:9090            | Status → Targets, Alerts        |
| Exporter   | http://localhost:9953/metrics    | Rohdaten des Exporters          |
| Loki       | http://localhost:3100/ready      | Empfang der db2diag-Einträge    |

## Dashboards

**Db2 Übersicht** (Startseite), filterbar nach Datenbank und Member:

| Bereich | Inhalt |
|---|---|
| Status | Erreichbarkeit, DB-Status, Uptime, Alter des letzten Backups, Version, fehlerhafte Exporter-Queries |
| Verbindungen | verbunden/aktiv/Höchststand, Verbindungen je Applikation, neue Verbindungen pro Sekunde |
| Bufferpools & I/O | Hit-Ratio für Daten und Index je Bufferpool, logische und physische Reads, Writes, Ø Lesezeit |
| Tablespaces & Logs | Belegung der Tablespaces, Dateisysteme der Storage-Pfade, Logspace in %, Log-Schreibrate und -latenz |
| Locks & Sorts | Lock-Waits, Timeouts, Deadlocks, Eskalationen, gehaltene/wartende Locks, aktuelle Lock-Waits je Tabelle, Sort-Overflows |
| SQL-Durchsatz | Commits/Rollbacks, Statements/s, Zeilen gelesen/zurück/geändert, Read-Efficiency, CPU, Wartezeit-Anteil |
| HADR | Status je Standby (Rolle, Syncmode, State, Verbindung, Hosts), Log-Gap, Empfangs- und Replay-Verzögerung, Wartezeit des Primary, Receive-Buffer/Spool, verpasste Heartbeats |
| Exporter | Laufzeit und Status jeder Abfrage |

**Db2 Top SQL**: die 20 teuersten Statements aus dem Package Cache (nach `TOTAL_ACT_TIME`) mit Ausführungen, Ø-Zeit, CPU, Wartezeit, Lock-Wartezeit und Zeilen. Dazu die Statements, die gerade die meiste Last erzeugen.

Die Dashboards sind in Grafana editierbar. Änderungen landen dann in der Grafana-Datenbank. Damit sie dauerhaft im Repo bleiben: *Export → Save to file* und die Datei unter `config/grafana/dashboards/` ersetzen.

**Db2 diag.log**: Anzahl Critical/Severe/Error/Warning, Einträge je Level als Balken, die häufigsten ADM/SQL-Meldungen und die Einträge selbst (aufklappbar mit allen Feldern). Filter nach Host, Instanz, Level und Suchtext (Regex).

## Alerts

`config/prometheus/rules/db2-alerts.yml` enthält:

| Alert | Bedingung |
|---|---|
| Db2Down | keine Verbindung seit 2 Minuten |
| Db2ExporterQueryFailing | eine Query schlägt seit 10 Minuten fehl (meist fehlende Rechte) |
| Db2TablespaceFull | DMS-Tablespace > 90 % von MAXSIZE bzw. der nutzbaren Größe (ohne AUTORESIZE) |
| Db2StoragePathFull | Dateisystem eines Storage-Pfads > 90 % |
| Db2LogSpaceHigh | aktiver Logspace > 80 % |
| Db2Deadlocks / Db2LockTimeouts | Deadlocks bzw. mehr als 5 Lock-Timeouts in 10 Minuten |
| Db2BufferpoolHitRatioLow | Hit-Ratio < 80 % bei nennenswerter Last |
| Db2BackupTooOld | letztes Backup älter als 2 Tage (nicht für `env=test`) |
| Db2HadrDisconnected | Standby seit 2 Minuten nicht verbunden |
| Db2HadrNotPeer | verbunden, aber seit 10 Minuten nicht PEER (SUPERASYNC ausgenommen) |
| Db2HadrLogGapHigh | Log-Gap > 256 MB |
| Db2HadrReplayDelayHigh | Standby spielt Logs mehr als 10 Minuten verzögert ein |
| Db2HadrHeartbeatsMissed | mehr als 2 verpasste Heartbeats in 10 Minuten |
| Db2HadrStandbyBufferFull | Receive-Buffer > 90 % oder Spool > 80 % |
| Db2HadrPrimaryWaiting | Primary wartet seit mehr als 5 s auf den Standby (Commits blockiert) |
| Db2HadrRoleChanged | Takeover: Primary-Host hat in den letzten 15 Minuten gewechselt |

Die Alerts sind in Prometheus und in Grafana sichtbar. Für Benachrichtigungen (Mail, Teams …) einen Alertmanager ergänzen oder in Grafana Contact Points einrichten.

## HADR

HADR wird über `MON_GET_HADR` am **Primary** überwacht. Das liefert eine Zeile je Standby (Principal und Auxiliary) und je Log-Stream. Ist HADR nicht konfiguriert, bleiben die HADR-Panels leer, und es werden keine HADR-Alerts ausgelöst.

- **Target**: In `targets.yaml` den Primary eintragen. Ein Standby ohne *Reads on Standby* nimmt keine Verbindungen an.
- **Takeover**: Ist auf dem Server *Automatic Client Reroute* eingerichtet (`db2 update alternate server for database ...`), folgt der Exporter nach einem Takeover automatisch dem neuen Primary. Der Alert `Db2HadrRoleChanged` meldet den Wechsel. Ohne ACR meldet `Db2Down` den alten Primary als nicht erreichbar.
- **Status-Labels**: Rolle, State und Verbindung stehen nur in `db2_hadr_info`. Die Zahlen-Metriken tragen nur stabile Labels (`standby_id`, `standby_host`, `syncmode`). So entstehen bei einem Statuswechsel keine neuen Zeitreihen.
- **HADR_REPLAY_DELAY**: Ist eine Replay-Verzögerung absichtlich konfiguriert, den Schwellwert in `Db2HadrReplayDelayHigh` entsprechend erhöhen.

## db2diag.log

Der Shipper (`diag-shipper/db2diag_shipper.py`) läuft auf dem Db2-Server als Instanzbesitzer. Er startet `db2diag -f -json`, liest die JSON-Einträge laufend mit und schickt sie gesammelt an Loki. Er braucht nur Python 3.7 oder neuer, keine weiteren Pakete. Bricht `db2diag` ab, startet er es neu. Ist Loki nicht erreichbar, puffert er bis zu 20.000 Einträge und sendet sie später.

**Was in Loki ankommt:**
- Die Logzeile ist der komplette JSON-Eintrag. In Grafana Explore lässt sich damit per `| json` nach jedem Feld filtern.
- Labels: `job=db2diag`, `host`, `db2_instance`, `db`, `member`, `level` (klein geschrieben: critical, severe, error, warning, info, event) und die Werte aus `EXTRA_LABELS`.
- Zusätzliche Felder (Structured Metadata): `function`, `pid`, `tid`, `eduid`, `eduname`, `appid`, `apphdl`, `proc` und `msgid`. `msgid` ist die erste ADMxxxx- bzw. SQLxxxx-Nummer im Eintrag.
- Der Zeitstempel kommt aus dem Eintrag selbst. Ist er älter als 6 Tage, wird die aktuelle Zeit verwendet, weil Loki so alte Einträge ablehnt. Der Originalwert steht dann in `db2_timestamp`.

Die Feldnamen der JSON-Ausgabe werden unabhängig von der Schreibweise erkannt (`LEVEL`, `level`, `DB`, `dbname` …). Beim Start schreibt der Shipper die Felder des ersten Eintrags ins Log. Fehlt im Dashboard etwas, sieht man dort, wie die Felder heißen.

### Installation auf einem Linux-Db2-Server (systemd)

```bash
cd diag-shipper
sudo install -d /opt/db2diag-shipper
sudo install -m 0755 db2diag_shipper.py /opt/db2diag-shipper/
sudo install -m 0600 -o db2inst1 db2diag-shipper.env.example /etc/db2diag-shipper.env
sudo vi /etc/db2diag-shipper.env          # LOKI_URL=http://<monitoring-host>:3100
sudo install -m 0644 systemd/db2diag-shipper@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now db2diag-shipper@db2inst1     # @<Instanzbesitzer>
journalctl -u db2diag-shipper@db2inst1 -f
```

Ohne systemd (z. B. zum Testen oder unter AIX):

```bash
. ~/sqllib/db2profile
LOKI_URL=http://<monitoring-host>:3100 nohup python3 db2diag_shipper.py > db2diag_shipper.log 2>&1 &
```

Der Port 3100 des Monitoring-Hosts muss vom Db2-Server erreichbar sein. Loki läuft hier ohne Anmeldung. Wenn der Port außerhalb eines vertrauenswürdigen Netzes erreichbar ist, einen Reverse Proxy mit Basic Auth/TLS davorsetzen. Der Shipper unterstützt das über `LOKI_USERNAME`, `LOKI_PASSWORD` und `LOKI_CA_FILE`.

### Test-Datenbank

```bash
./scripts/testdb-diag-shipper.sh
```

Das Skript kopiert den Shipper in den Container `db2-test` und startet ihn dort als `db2inst1`. Dafür braucht der Container `python3`. Nach einem Neustart des Containers das Skript erneut ausführen.

### Alerts aus dem diag.log

`config/loki/rules/fake/db2diag-alerts.yml` (Loki Ruler, in Grafana unter *Alerting* sichtbar):

| Alert | Bedingung |
|---|---|
| Db2DiagSevere | mindestens ein Severe- oder Critical-Eintrag in 5 Minuten |
| Db2DiagErrorsHigh | mehr als 20 Errors in 15 Minuten |
| Db2DiagSilent | 6 Stunden lang kein Eintrag von einem Host (Shipper läuft nicht?) |

## Berechtigungen

Der Exporter braucht nur Lesezugriff auf die Monitoring-Funktionen. Benutzer `DB2MON` anlegen (OS oder LDAP), dann als DBADM bzw. SECADM:

```bash
db2 connect to PRODDB
db2 -tvf sql/grants.sql
```

Variante A vergibt nur `EXECUTE` auf die benötigten Funktionen (empfohlen). Variante B nutzt `SQLADM`.

Fehlt ein Recht, liefert nur die betroffene Query keine Daten. Das zeigt das Panel *Fehlerhafte Queries* bzw. `podman logs db2-exporter`.

## Konfiguration

### Mehrere Datenbanken

In `config/exporter/targets.yaml` weitere Einträge unter `targets:` anlegen, jeweils mit eigenem `name` und eigener Passwort-Variable (`password_env`). Die Variablen kommen in `.env`. Danach `podman restart db2-exporter`.

### SSL/TLS

```yaml
    ssl: true
    ssl_server_certificate: /etc/db2-exporter/certs/db2-server.arm
```

Das Server-Zertifikat (ARM/PEM) nach `config/exporter/certs/` legen. Der Ordner ist per `.gitignore` vom Commit ausgeschlossen.

### Eigene Metriken

Neue Query in `config/exporter/queries.yaml` eintragen:

```yaml
  - name: table_activity
    sql: |
      SELECT TABSCHEMA, TABNAME, ROWS_READ, ROWS_INSERTED, ROWS_UPDATED, ROWS_DELETED
      FROM TABLE(MON_GET_TABLE('', '', -2)) AS T
      WHERE TABSCHEMA NOT LIKE 'SYS%'
      ORDER BY ROWS_READ DESC FETCH FIRST 20 ROWS ONLY
    labels:
      tabschema: schema
      tabname: table
    metrics:
      - {column: rows_read, name: db2_table_rows_read, help: Gelesene Zeilen je Tabelle}
      - {column: rows_inserted, name: db2_table_rows_inserted, help: Eingefuegte Zeilen je Tabelle}
```

Danach `podman restart db2-exporter`. Spaltennamen sind unabhängig von Groß-/Kleinschreibung. Alle Metriken bekommen automatisch das Label `db`.

### Wichtige Variablen (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PROMETHEUS_RETENTION` | `30d` | Aufbewahrung der Metriken |
| `GRAFANA_VERSION` / `PROMETHEUS_VERSION` | `12.1.0` / `v3.5.0` | Image-Versionen |
| `DB2_TEST_IMAGE` | `icr.io/db2_community/db2:latest` | Image der Test-DB |
| `LOKI_RETENTION` | `30d` | Aufbewahrung der db2diag-Einträge |
| `LOG_LEVEL` | `INFO` | `DEBUG` für mehr Exporter-Ausgaben |

## Betrieb

```bash
podman compose ps                     # Status
podman logs db2-exporter              # Exporter-Log (Verbindungs- und SQL-Fehler)
curl -s localhost:9953/metrics | grep db2_up
curl -X POST localhost:9090/-/reload  # Prometheus-Konfiguration neu laden
podman compose down                   # stoppen (Daten bleiben in Volumes)
podman compose down -v                # stoppen und alle Daten löschen
```

## Hinweise

- **Kumulative Werte**: Die meisten `MON_GET_*`-Werte zählen seit Aktivierung der Datenbank hoch. Prometheus rechnet daraus Raten. Nach einer Deaktivierung oder einem Neustart beginnen die Zähler neu, `rate()` berücksichtigt das.
- **Top-SQL-Werte** zählen seit Aufnahme des Statements in den Package Cache. Verdrängte Statements verschwinden aus der Liste.
- **Last auf der Datenbank**: Die Abfragen laufen alle 30 Sekunden und sind leichtgewichtig. Am teuersten ist `MON_GET_PKG_CACHE_STMT` bei einem sehr großen Package Cache. Bei Bedarf das Scrape-Intervall in `config/prometheus/prometheus.yml` erhöhen oder die Query `top_sql` entfernen.
- **ARM64** (z. B. Apple Silicon) wird vom IBM-Treiber in `ibm_db` nicht unterstützt. Dort den Exporter mit `--platform linux/amd64` bauen.
- **SELinux**: Die Volumes sind mit `:Z` gemountet und funktionieren deshalb unter Podman auf Fedora/RHEL.

## Debugging

Möchte man wissen warum Werte auf dem Board offensichtlich nicht stimmen so kann man sich 
- das Log anschauen
- wenn bspw. eine DB nicht erreichbar ist, diesen Check interaktiv ausführen
- 
### Log anschauen

```bash
podman logs --tail 50 db2-exporter
```

### interaktiv

```bash
podman exec -it db2-exporter python -c "
import os, ibm_db
cs = f\"DATABASE={os.environ['DB2_DATABASE']};HOSTNAME={os.environ['DB2_HOST']};PORT={os.environ.get('DB2_PORT','50000')};PROTOCOL=TCPIP;UID={os.environ['DB2_USER']};PWD={os.environ['DB2_PASSWORD']};\"
print('Ziel:', os.environ['DB2_HOST'], os.environ.get('DB2_PORT'))
c = ibm_db.connect(cs, '', ''); print('Verbindung OK')"
```

## Struktur

```
compose.yaml                     Exporter, Prometheus, Grafana
compose.testdb.yaml              optionale Db2-Testdatenbank
.env.example                     Vorlage für Zugangsdaten und Ports
exporter/                        Containerfile + Python-Exporter
config/exporter/                 targets.yaml, targets.testdb.yaml, queries.yaml, certs/
config/prometheus/               prometheus.yml, rules/db2-alerts.yml
config/loki/                     loki.yaml, rules/fake/db2diag-alerts.yml
config/grafana/                  Provisioning + Dashboards (JSON)
diag-shipper/                    db2diag-Shipper, systemd-Unit, Beispiel-Konfiguration
sql/grants.sql                   Rechte für den Monitoring-Benutzer
scripts/workload.sh              Testlast für die Test-Datenbank
scripts/testdb-diag-shipper.sh   Shipper im Test-Container starten
sidecar/                         eigener Stack: Db2 + db2diag-Sidecar + Monitor
```
