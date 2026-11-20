#!/usr/bin/env python3
"""
Db2 LUW Prometheus Exporter.

Liest Ziel-Datenbanken (targets.yaml) und SQL-Abfragen (queries.yaml),
fuehrt die Abfragen bei jedem Scrape aus und liefert die Ergebnisse im
Prometheus-Textformat unter /metrics aus.

Abhaengigkeiten: ibm_db, PyYAML (keine weiteren).
"""

import logging
import os
import re
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

try:
    import ibm_db  # noqa: F401
except ImportError:  # erlaubt Tests ohne Treiber
    ibm_db = None

LOG = logging.getLogger("db2_exporter")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

def _expand_env(value):
    """Ersetzt ${VAR} und ${VAR:-default} rekursiv in Strings."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return _expand_env(yaml.safe_load(fh) or {})


def validate_queries(queries):
    names = set()
    for q in queries:
        for key in ("name", "sql", "metrics"):
            if key not in q:
                raise ValueError(f"Query ohne '{key}': {q}")
        if q["name"] in names:
            raise ValueError(f"Query-Name doppelt: {q['name']}")
        names.add(q["name"])
        for col, label in (q.get("labels") or {}).items():
            if not _LABEL_NAME.match(label):
                raise ValueError(f"Ungueltiger Label-Name '{label}' in Query {q['name']}")
        for m in q["metrics"]:
            if not _METRIC_NAME.match(m["name"]):
                raise ValueError(f"Ungueltiger Metrik-Name '{m['name']}' in Query {q['name']}")
            if m.get("type", "gauge") not in ("gauge", "counter"):
                raise ValueError(f"Typ muss gauge oder counter sein: {m['name']}")
            if "column" not in m:
                raise ValueError(f"Metrik ohne 'column': {m['name']}")
    return queries


# --------------------------------------------------------------------------
# Prometheus-Textformat
# --------------------------------------------------------------------------

def _escape_label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels.items()) + "}"


def _format_value(value):
    if value != value:  # NaN
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


class MetricRegistry:
    """Sammelt Samples eines Scrapes und rendert sie gruppiert pro Metrik."""

    def __init__(self):
        self._meta = {}
        self._samples = {}

    def add(self, name, value, labels=None, help_text="", mtype="gauge"):
        if name not in self._meta:
            self._meta[name] = (help_text or name, mtype)
            self._samples[name] = []
        self._samples[name].append((labels or {}, value))

    def render(self):
        out = []
        for name, (help_text, mtype) in self._meta.items():
            out.append(f"# HELP {name} {help_text.replace(chr(10), ' ')}")
            out.append(f"# TYPE {name} {mtype}")
            for labels, value in self._samples[name]:
                out.append(f"{name}{_format_labels(labels)} {_format_value(value)}")
        return "\n".join(out) + "\n"


def _to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _clean_label_value(value, max_len=150):
    text = "" if value is None else re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


# --------------------------------------------------------------------------
# Db2-Zugriff
# --------------------------------------------------------------------------

class Db2Target:
    def __init__(self, cfg, query_timeout):
        self.name = cfg["name"]
        self.cfg = cfg
        self.extra_labels = {k: str(v) for k, v in (cfg.get("labels") or {}).items()}
        self.query_timeout = query_timeout
        self._conn = None
        self._lock = threading.Lock()
        self.error_totals = {}

    def _conn_string(self):
        c = self.cfg
        password = os.environ.get(c["password_env"], "") if c.get("password_env") else c.get("password", "")
        parts = [
            f"DATABASE={c['database']}",
            f"HOSTNAME={c['host']}",
            f"PORT={c.get('port', 50000)}",
            "PROTOCOL=TCPIP",
            f"UID={c['user']}",
            f"PWD={password}",
            f"ConnectTimeout={c.get('connect_timeout', 10)}",
        ]
        if c.get("ssl"):
            parts.append("SECURITY=SSL")
            if c.get("ssl_server_certificate"):
                parts.append(f"SSLServerCertificate={c['ssl_server_certificate']}")
        return ";".join(parts) + ";"

    def _connect(self):
        if self._conn is not None:
            try:
                if ibm_db.active(self._conn):
                    return self._conn
            except Exception:  # noqa: BLE001
                pass
            self._close()
        LOG.info("Verbinde zu %s (%s:%s/%s)", self.name, self.cfg["host"],
                 self.cfg.get("port", 50000), self.cfg["database"])
        self._conn = ibm_db.connect(self._conn_string(), "", "")
        return self._conn

    def _close(self):
        if self._conn is not None:
            try:
                ibm_db.close(self._conn)
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def _run(self, sql):
        conn = self._connect()
        options = {}
        timeout_attr = getattr(ibm_db, "SQL_ATTR_QUERY_TIMEOUT", None)
        if timeout_attr is not None and self.query_timeout:
            options[timeout_attr] = self.query_timeout
        stmt = ibm_db.prepare(conn, sql, options) if options else ibm_db.prepare(conn, sql)
        ibm_db.execute(stmt)
        rows = []
        row = ibm_db.fetch_assoc(stmt)
        while row:
            rows.append({str(k).lower(): v for k, v in row.items()})
            row = ibm_db.fetch_assoc(stmt)
        return rows

    def _healthy(self):
        try:
            self._run("SELECT 1 FROM SYSIBM.SYSDUMMY1")
            return True
        except Exception:  # noqa: BLE001
            return False

    def collect(self, queries, registry):
        base = {"db": self.name, **self.extra_labels}
        start = time.monotonic()
        with self._lock:
            try:
                self._connect()
                up = 1
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Verbindung zu %s fehlgeschlagen: %s", self.name, exc)
                self._close()
                up = 0

            if up:
                for q in queries:
                    self._collect_query(q, base, registry)

            registry.add("db2_up", up, base, "1 wenn die Verbindung zur Datenbank funktioniert")
            registry.add("db2_exporter_scrape_duration_seconds", time.monotonic() - start, base,
                         "Dauer des Scrapes fuer diese Datenbank")
            for qname, total in self.error_totals.items():
                registry.add("db2_exporter_query_errors_total", total, {**base, "query": qname},
                             "Fehlgeschlagene Ausfuehrungen je Query", "counter")

    def _collect_query(self, q, base, registry):
        qstart = time.monotonic()
        self.error_totals.setdefault(q["name"], 0)
        try:
            rows = self._run(q["sql"])
            ok = 1
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Query %s auf %s fehlgeschlagen: %s", q["name"], self.name, exc)
            self.error_totals[q["name"]] += 1
            rows, ok = [], 0
            if not self._healthy():
                self._close()

        label_map = q.get("labels") or {}
        max_len = q.get("label_max_length", 150)
        for row in rows:
            labels = dict(base)
            for col, label in label_map.items():
                labels[label] = _clean_label_value(row.get(col.lower()), max_len)
            for m in q["metrics"]:
                value = _to_float(row.get(m["column"].lower()))
                if value is None:
                    continue
                registry.add(m["name"], value, labels, m.get("help", ""), m.get("type", "gauge"))

        qlabels = {**base, "query": q["name"]}
        registry.add("db2_exporter_query_success", ok, qlabels, "1 wenn die letzte Ausfuehrung der Query erfolgreich war")
        registry.add("db2_exporter_query_duration_seconds", time.monotonic() - qstart, qlabels,
                     "Laufzeit der Query in Sekunden")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Exporter:
    def __init__(self, targets_file, queries_file):
        targets_cfg = load_yaml(targets_file)
        queries_cfg = load_yaml(queries_file)
        timeout = int(targets_cfg.get("query_timeout_seconds", 20))
        self.targets = [Db2Target(t, timeout) for t in targets_cfg.get("targets", [])]
        if not self.targets:
            raise ValueError(f"Keine Targets in {targets_file}")
        self.queries = validate_queries(queries_cfg.get("queries", []))
        LOG.info("%d Target(s), %d Queries geladen", len(self.targets), len(self.queries))

    def scrape(self):
        registry = MetricRegistry()
        # Targets parallel abfragen, jedes in eigene Registry, danach zusammenfuehren
        registries = [MetricRegistry() for _ in self.targets]
        threads = [threading.Thread(target=t.collect, args=(self.queries, r))
                   for t, r in zip(self.targets, registries)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        for r in registries:
            for name, (help_text, mtype) in r._meta.items():  # noqa: SLF001
                for labels, value in r._samples[name]:  # noqa: SLF001
                    registry.add(name, value, labels, help_text, mtype)
        return registry.render()


def make_handler(exporter):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/metrics"):
                body = exporter.scrape().encode("utf-8")
                ctype = "text/plain; version=0.0.4; charset=utf-8"
                status = 200
            elif self.path in ("/", "/health"):
                body = b"db2-exporter ok - Metriken unter /metrics\n"
                ctype, status = "text/plain; charset=utf-8", 200
            else:
                body, ctype, status = b"not found\n", "text/plain", 404
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            LOG.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if ibm_db is None:
        LOG.error("Modul ibm_db nicht installiert")
        sys.exit(1)
    targets_file = os.environ.get("EXPORTER_TARGETS_FILE", "/etc/db2-exporter/targets.yaml")
    queries_file = os.environ.get("EXPORTER_QUERIES_FILE", "/etc/db2-exporter/queries.yaml")
    port = int(os.environ.get("EXPORTER_PORT", "9953"))
    exporter = Exporter(targets_file, queries_file)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(exporter))
    LOG.info("Lausche auf Port %d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
