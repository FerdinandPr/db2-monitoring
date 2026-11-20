# Db2 LUW Monitoring mit Prometheus und Grafana

Container-basierte Monitoring-Umgebung für Db2 LUW (11.1 oder neuer):

```
Db2  ──SQL (MON_GET_*)──▶  db2-exporter  ──/metrics──▶  Prometheus  ──▶  Grafana
                           (Python, ibm_db)              (+ Alert-Regeln)   (2 Dashboards)
```

- **db2-exporter**: kleiner Python-Exporter. Er führt die SQL-Abfragen aus `config/exporter/queries.yaml` bei jedem Scrape aus. Mehrere Datenbanken sind möglich.
- **Prometheus**: speichert die Metriken (Standard: 30 Tage) und wertet die Alert-Regeln aus.
- **Grafana**: Datasource und Dashboards werden beim Start automatisch eingerichtet (Provisioning).
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

### Aufrufen

| Dienst     | URL                              | Hinweis                         |
|------------|----------------------------------|---------------------------------|
| Grafana    | http://localhost:3000            | Login aus `.env` (Standard admin/admin) |
| Prometheus | http://localhost:9090            | Status → Targets, Alerts        |
| Exporter   | http://localhost:9953/metrics    | Rohdaten des Exporters          |

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
| Exporter | Laufzeit und Status jeder Abfrage |

**Db2 Top SQL**: die 20 teuersten Statements aus dem Package Cache (nach `TOTAL_ACT_TIME`) mit Ausführungen, Ø-Zeit, CPU, Wartezeit, Lock-Wartezeit und Zeilen. Dazu die Statements, die gerade die meiste Last erzeugen.

Die Dashboards sind in Grafana editierbar. Änderungen landen dann in der Grafana-Datenbank. Damit sie dauerhaft im Repo bleiben: *Export → Save to file* und die Datei unter `config/grafana/dashboards/` ersetzen.

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

Die Alerts sind in Prometheus und in Grafana sichtbar. Für Benachrichtigungen (Mail, Teams …) einen Alertmanager ergänzen oder in Grafana Contact Points einrichten.

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
  - name: hadr
    sql: |
      SELECT HADR_ROLE, HADR_STATE, HADR_LOG_GAP
      FROM TABLE(MON_GET_HADR(-2)) AS T
    labels:
      hadr_role: role
      hadr_state: state
    metrics:
      - {column: hadr_log_gap, name: db2_hadr_log_gap_bytes, help: HADR Log-Gap}
```

Danach `podman restart db2-exporter`. Spaltennamen sind unabhängig von Groß-/Kleinschreibung. Alle Metriken bekommen automatisch das Label `db`.

### Wichtige Variablen (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PROMETHEUS_RETENTION` | `30d` | Aufbewahrung der Metriken |
| `GRAFANA_VERSION` / `PROMETHEUS_VERSION` | `12.1.0` / `v3.5.0` | Image-Versionen |
| `DB2_TEST_IMAGE` | `icr.io/db2_community/db2:latest` | Image der Test-DB |
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

## Struktur

```
compose.yaml                     Exporter, Prometheus, Grafana
compose.testdb.yaml              optionale Db2-Testdatenbank
.env.example                     Vorlage für Zugangsdaten und Ports
exporter/                        Containerfile + Python-Exporter
config/exporter/                 targets.yaml, targets.testdb.yaml, queries.yaml, certs/
config/prometheus/               prometheus.yml, rules/db2-alerts.yml
config/grafana/                  Provisioning + Dashboards (JSON)
sql/grants.sql                   Rechte für den Monitoring-Benutzer
scripts/workload.sh              Testlast für die Test-Datenbank
```
