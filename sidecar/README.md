# Sidecar-Stack: Db2 + db2diag-Sidecar + Monitor

Ein eigenständiges Compose-Projekt im Ordner `sidecar/`. Ein Befehl startet alles, und es ist fertig eingerichtet:

```
┌─ Db2-Container (sc-db2) ───────┐        Volume db2-data (read-only)
│  Db2 Community, TESTDB          │ ──────────────────────────┐
│  .../sqllib/db2dump/db2diag.log │                           ▼
└──────────────┬──────────────────┘              db2diag-sidecar ──push──▶ Loki ──┐
               │ SQL (MON_GET_*)                 (liest db2diag.log)               ├──▶ Grafana
               ▼                                                                    │
          db2-exporter ──/metrics──▶ Prometheus ───────────────────────────────────┘
```

- **db2** (`sc-db2`): Db2 Community Edition. Mit `PERSISTENT_HOME=true` liegen das Home von `db2inst1` und damit `sqllib/db2dump` im Volume `db2-data`.
- **db2diag-sidecar**: bindet dasselbe Volume **read-only** ein und liest `db2diag.log` direkt als Datei. Die Datei braucht keinen `db2diag`-Befehl und keine Db2-Installation. Jeder Eintrag wird in seine Felder zerlegt (`TIMESTAMP`, `LEVEL`, `PID`, `INSTANCE`, `NODE`, `DB`, `FUNCTION`, `MESSAGE` …) und mit den gleichen Labels wie beim normalen Shipper an Loki geschickt. Die Dashboards und Loki-Alerts funktionieren deshalb unverändert.
- **Monitor**: Exporter, Prometheus, Loki und Grafana mit den Konfigurationen aus dem Hauptprojekt (`../config`). Die Datenbank ist in `config/exporter/targets.yaml` schon als Target eingetragen.

## Start

Voraussetzungen wie im Hauptprojekt: Podman (rootful Machine unter Windows/macOS, weil Db2 `privileged` braucht) auf x86_64.

```bash
cd sidecar
podman compose up -d --build
podman logs -f sc-db2                 # warten bis "Setup has completed" (einige Minuten)
```

Eine `.env` ist nicht nötig. Für eigene Passwörter oder Ports: `cp .env.example .env`.

Testlast erzeugen, damit die Graphen Daten zeigen:

```bash
DB2_CONTAINER=sc-db2 ../scripts/workload.sh 100
```

## Aufrufen

| Dienst     | URL                           | Hinweis |
|------------|-------------------------------|---------|
| Grafana    | http://localhost:3000         | admin/admin. Dashboards *Db2 Übersicht*, *Db2 Top SQL*, *Db2 diag.log* |
| Prometheus | http://localhost:9090         | Status → Targets |
| Exporter   | http://localhost:9953/metrics | |
| Loki       | http://localhost:3100/ready   | |
| Db2        | localhost:50000               | `db2inst1` / Passwort aus `DB2_PASSWORD` |

Die Ports sind dieselben wie beim Hauptprojekt. Für einen Betrieb beider Stacks gleichzeitig die Ports in `.env` ändern. Die Containernamen beginnen mit `sc-`, deshalb gibt es keine Namenskonflikte.

## Der Sidecar

`diag-sidecar/db2diag_file_shipper.py` nutzt `RecordBuilder` und `LokiSender` aus `../diag-shipper/db2diag_shipper.py`. Neu ist nur das Lesen der Datei:

- **Datei finden**: Standard ist `/database/config/*/sqllib/db2dump/DIAG*/db2diag.log`, danach `.../db2dump/db2diag.log`. Bei mehreren Treffern nimmt er die zuletzt geänderte Datei. Solange die Datei fehlt (Db2-Setup läuft), wartet er.
- **Einträge erkennen**: Ein Eintrag beginnt mit dem Zeitstempel (`2026-11-26-11.00.01.123456+060 I1234E456  LEVEL: Info`). Er gilt als fertig, wenn der nächste beginnt oder 2 Sekunden lang nichts dazukommt. So werden halb geschriebene Einträge nicht zerrissen.
- **Logzeile in Loki**: JSON mit den erkannten Feldern, `RAW` enthält den kompletten Originaleintrag inklusive `DATA #n`-Blöcken.
- **Position**: wird im Volume `sidecar-state` gespeichert. Nach einem Neustart des Sidecars kommt nichts doppelt.
- **Rotation**: Eine neue Datei (anderer Inode, z. B. nach `db2diag -A` oder durch `DIAGSIZE`) wird erkannt. Der Rest der alten Datei wird vorher noch gelesen. Wird die Datei gekürzt, liest er von vorne.

Einstellungen (Umgebungsvariablen im Service `db2diag-sidecar`):

| Variable | Standard | Bedeutung |
|---|---|---|
| `LOKI_URL` | `http://loki:3100` | Ziel |
| `HOST_LABEL` | `db2-sidecar` | Label `host` |
| `EXTRA_LABELS` | `env=test` | weitere Labels |
| `DB2DIAG_FILE` | siehe oben | Pfad oder Glob-Muster, mehrere durch Komma |
| `START_AT` | `beginning` | `beginning` oder `end`, gilt nur beim allerersten Start |
| `STATE_FILE` | `/state/position.json` | gespeicherte Leseposition |

Dazu die Variablen des normalen Shippers (`BATCH_SIZE`, `FLUSH_SECONDS`, `MAX_BUFFER`, `LOKI_USERNAME` …).

Der Sidecar läuft als root, damit er `db2diag.log` unabhängig von UID und Rechten des Instanzbesitzers lesen kann. Das Volume ist read-only eingebunden.

## Aufräumen

```bash
podman compose down          # Container weg, Daten bleiben
podman compose down -v       # inklusive Volumes (Datenbank wird beim nächsten Start neu angelegt)
```
