#!/usr/bin/env python3
"""
db2diag-shipper: liest "db2diag -f -json" fortlaufend und schickt jeden
Eintrag an Grafana Loki.

Laeuft auf dem Db2-Server als Instanzbesitzer (db2diag muss im PATH sein).
Nur Python-Standardbibliothek, Python 3.7 oder neuer.

Konfiguration ueber Umgebungsvariablen:
  LOKI_URL            z. B. http://monitoring-host:3100   (Pflicht)
  DB2DIAG_CMD         Standard: "db2diag -f -json"
  HOST_LABEL          Wert des Labels "host" (Standard: Hostname)
  EXTRA_LABELS        zusaetzliche Labels, z. B. "env=prod,team=dba"
  LOKI_TENANT         optional, Header X-Scope-OrgID
  LOKI_USERNAME / LOKI_PASSWORD   optional, Basic Auth
  LOKI_CA_FILE        optional, CA-Datei fuer HTTPS
  BATCH_SIZE          Standard 200 Eintraege
  FLUSH_SECONDS       Standard 2
  MAX_BUFFER          Standard 20000 Eintraege (aelteste werden verworfen)
  LOG_LEVEL           INFO | DEBUG
"""

import base64
import codecs
import collections
import datetime as dt
import json
import logging
import os
import re
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("db2diag-shipper")

# Db2-Zeitstempel: 2026-11-25-13.09.12.123456+060 (Offset in Minuten)
_DB2_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)-(\d\d)\.(\d\d)\.(\d\d)\.(\d{1,9})([+-]\d{1,4})?$")
_MSGID = re.compile(r"\b(ADM\d{4,5}[A-Z]|SQL\d{4,5}[A-Z]|DBI\d{4,5}[A-Z]|SQL\d{4,5})\b")
_LEVELS = {"info": "info", "informational": "info", "warning": "warning", "warn": "warning",
           "error": "error", "severe": "severe", "critical": "critical", "event": "event",
           "i": "info", "w": "warning", "e": "error", "s": "severe", "c": "critical"}
MAX_LINE_BYTES = 200_000
OLDEST_ACCEPTED = dt.timedelta(days=6)  # Loki lehnt standardmaessig Eintraege > 7 Tage ab


# --------------------------------------------------------------------------
# Feldzugriff unabhaengig von Schreibweise (LEVEL, level, Level, db_name ...)
# --------------------------------------------------------------------------

def _norm(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _flatten(obj, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}{k}.", out)
    elif not isinstance(obj, list):
        key = prefix[:-1]
        out.setdefault(_norm(key.rsplit(".", 1)[-1]), obj)
        out.setdefault(_norm(key), obj)
    return out


def _pick(flat, *candidates):
    for c in candidates:
        v = flat.get(_norm(c))
        if v not in (None, ""):
            return str(v).strip()
    return ""


def parse_timestamp(value):
    """Liefert datetime (UTC) oder None."""
    if not value:
        return None
    value = str(value).strip()
    m = _DB2_TS.match(value)
    if m:
        y, mo, d, h, mi, s, frac, off = m.groups()
        micro = int(frac[:6].ljust(6, "0"))
        offset = dt.timedelta(minutes=int(off)) if off else dt.timedelta(0)
        local = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro)
        return (local - offset).replace(tzinfo=dt.timezone.utc)
    try:
        ts = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.astimezone()  # lokale Zeit des Servers
        return ts.astimezone(dt.timezone.utc)
    except ValueError:
        return None


class Record:
    __slots__ = ("labels", "ts_ns", "line", "meta")

    def __init__(self, labels, ts_ns, line, meta):
        self.labels, self.ts_ns, self.line, self.meta = labels, ts_ns, line, meta


class RecordBuilder:
    def __init__(self, static_labels):
        self.static_labels = static_labels
        self._last_ns = 0
        self._seen_keys = False

    def _unique_ns(self, ns):
        # gleiche Zeitstempel leicht versetzen, damit die Reihenfolge erhalten bleibt
        if ns <= self._last_ns:
            ns = self._last_ns + 1
        self._last_ns = ns
        return ns

    def from_json(self, obj):
        if not self._seen_keys:
            self._seen_keys = True
            keys = sorted(obj.keys()) if isinstance(obj, dict) else type(obj).__name__
            LOG.info("Erster Eintrag von db2diag, Felder: %s", keys)
        flat = _flatten(obj)
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        level_raw = _pick(flat, "level", "severity", "lvl")
        labels = dict(self.static_labels)
        labels["level"] = _LEVELS.get(level_raw.lower(), level_raw.lower() or "unknown")
        instance = _pick(flat, "instance", "instancename", "inst")
        if instance:
            labels["db2_instance"] = instance
        db = _pick(flat, "db", "dbname", "database", "databasename")
        if db:
            labels["db"] = db.upper()
        member = _pick(flat, "member", "node", "dbpartitionnum", "partition")
        if member:
            labels["member"] = str(int(member)) if member.isdigit() else member

        meta = {}
        for name, cands in (("function", ("function", "func")), ("pid", ("pid",)), ("tid", ("tid",)),
                            ("eduid", ("eduid",)), ("eduname", ("eduname",)), ("appid", ("appid", "applid", "applicationid")),
                            ("apphdl", ("apphdl", "applicationhandle")), ("proc", ("proc", "procname", "processname")),
                            ("hostname", ("hostname", "host"))):
            v = _pick(flat, *cands)
            if v:
                meta[name] = v[:200]
        msgid = _MSGID.search(line)
        if msgid:
            meta["msgid"] = msgid.group(1)
        raw_ts = _pick(flat, "timestamp", "time", "ts", "datetime", "recordtime")
        return self._finish(labels, raw_ts, line, meta)

    def from_text(self, text):
        labels = dict(self.static_labels)
        m = re.search(r"LEVEL:\s*(\w+)", text)
        labels["level"] = _LEVELS.get(m.group(1).lower(), m.group(1).lower()) if m else "unknown"
        meta = {"format": "text"}
        msgid = _MSGID.search(text)
        if msgid:
            meta["msgid"] = msgid.group(1)
        return self._finish(labels, text.split(" ", 1)[0], text, meta)

    def _finish(self, labels, raw_ts, line, meta):
        now = dt.datetime.now(dt.timezone.utc)
        ts = parse_timestamp(raw_ts)
        if ts is None or ts > now + dt.timedelta(minutes=5) or now - ts > OLDEST_ACCEPTED:
            if raw_ts:
                meta["db2_timestamp"] = raw_ts
            ts = now
        ns = self._unique_ns(int(ts.timestamp() * 1_000_000) * 1000)
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            line = line.encode("utf-8")[:MAX_LINE_BYTES].decode("utf-8", "ignore") + "...[gekuerzt]"
        return Record(labels, ns, line, meta)


# --------------------------------------------------------------------------
# Lesen der db2diag-Ausgabe (JSON je Zeile, mehrzeilig oder als Array)
# --------------------------------------------------------------------------

class StreamParser:
    """Zerlegt einen Zeichenstrom in JSON-Objekte; Nicht-JSON-Text wird zeilenweise geliefert."""

    def __init__(self):
        self._buf = ""
        self._decoder = json.JSONDecoder()

    def feed(self, chunk):
        self._buf += chunk
        out = []
        while True:
            stripped = self._buf.lstrip(" \t\r\n,[]")
            self._buf = stripped
            if not stripped:
                break
            if stripped[0] == "{":
                try:
                    obj, end = self._decoder.raw_decode(stripped)
                except json.JSONDecodeError:
                    if len(stripped) > 5 * MAX_LINE_BYTES:  # kaputtes Objekt -> bis zum naechsten '{' verwerfen
                        nxt = stripped.find("\n{", 1)
                        LOG.warning("Ungueltiges JSON verworfen (%d Zeichen)", nxt if nxt > 0 else len(stripped))
                        self._buf = stripped[nxt + 1:] if nxt > 0 else ""
                        continue
                    break  # unvollstaendig, auf mehr Daten warten
                out.append(("json", obj))
                self._buf = stripped[end:]
            else:
                nl = stripped.find("\n")
                if nl < 0:
                    break
                line = stripped[:nl].rstrip()
                self._buf = stripped[nl + 1:]
                if line:
                    out.append(("text", line))
        return out


# --------------------------------------------------------------------------
# Versand an Loki
# --------------------------------------------------------------------------

class LokiSender(threading.Thread):
    def __init__(self, url, batch_size, flush_seconds, max_buffer):
        super().__init__(daemon=True)
        self.url = url.rstrip("/") + "/loki/api/v1/push"
        self.batch_size, self.flush_seconds = batch_size, flush_seconds
        self.queue = collections.deque(maxlen=max_buffer)
        self.cond = threading.Condition()
        self.stopping = False
        self.sent = self.dropped = 0
        self.headers = {"Content-Type": "application/json"}
        if os.environ.get("LOKI_TENANT"):
            self.headers["X-Scope-OrgID"] = os.environ["LOKI_TENANT"]
        if os.environ.get("LOKI_USERNAME"):
            token = f"{os.environ['LOKI_USERNAME']}:{os.environ.get('LOKI_PASSWORD', '')}"
            self.headers["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
        ca = os.environ.get("LOKI_CA_FILE")
        self.ssl_ctx = ssl.create_default_context(cafile=ca) if ca else None

    def put(self, record):
        with self.cond:
            if len(self.queue) == self.queue.maxlen:
                self.dropped += 1
                if self.dropped % 1000 == 1:
                    LOG.warning("Puffer voll - %d Eintraege verworfen (Loki erreichbar?)", self.dropped)
            self.queue.append(record)
            if len(self.queue) >= self.batch_size:
                self.cond.notify()

    def _payload(self, batch):
        streams = collections.OrderedDict()
        for r in batch:
            key = tuple(sorted(r.labels.items()))
            streams.setdefault(key, []).append([str(r.ts_ns), r.line, r.meta] if r.meta else [str(r.ts_ns), r.line])
        return {"streams": [{"stream": dict(k), "values": v} for k, v in streams.items()]}

    def _post(self, batch):
        data = json.dumps(self._payload(batch), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=self.headers, method="POST")
        with urllib.request.urlopen(req, timeout=15, context=self.ssl_ctx) as resp:
            resp.read()

    def run(self):
        backoff = 1
        while True:
            with self.cond:
                if not self.queue and not self.stopping:
                    self.cond.wait(self.flush_seconds)
                if not self.queue:
                    if self.stopping:
                        return
                    continue
                batch = [self.queue.popleft() for _ in range(min(self.batch_size, len(self.queue)))]
            try:
                self._post(batch)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:300]
                if 400 <= exc.code < 500 and exc.code != 429:
                    LOG.error("Loki lehnt %d Eintraege ab (HTTP %d): %s", len(batch), exc.code, body)
                    continue
                LOG.warning("Loki HTTP %d, neuer Versuch in %ds: %s", exc.code, backoff, body)
                backoff = self._retry_later(batch, backoff)
                continue
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Loki nicht erreichbar (%s), neuer Versuch in %ds", exc, backoff)
                backoff = self._retry_later(batch, backoff)
                continue
            backoff = 1
            self.sent += len(batch)
            LOG.debug("%d Eintraege gesendet (gesamt %d)", len(batch), self.sent)

    def _retry_later(self, batch, backoff):
        with self.cond:
            self.queue.extendleft(reversed(batch))  # wieder vorne einreihen
            if self.stopping:
                self.queue.clear()  # beim Beenden nicht endlos warten
                return backoff
            self.cond.wait(backoff)
        return min(backoff * 2, 60)

    def stop(self, timeout=10):
        with self.cond:
            self.stopping = True
            self.cond.notify()
        self.join(timeout)


# --------------------------------------------------------------------------
# Hauptschleife
# --------------------------------------------------------------------------

def static_labels():
    labels = {"job": "db2diag", "host": os.environ.get("HOST_LABEL") or socket.gethostname()}
    for part in filter(None, (p.strip() for p in os.environ.get("EXTRA_LABELS", "").split(","))):
        if "=" in part:
            k, v = part.split("=", 1)
            labels[k.strip()] = v.strip()
    return labels


def run_command(cmd, builder, sender, stop_event):
    LOG.info("Starte: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    threading.Thread(target=_log_stderr, args=(proc,), daemon=True).start()
    parser = StreamParser()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while not stop_event.is_set():
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.readline()
            if not chunk:
                break
            for kind, item in parser.feed(decoder.decode(chunk)):
                rec = builder.from_json(item) if kind == "json" else builder.from_text(item)
                sender.put(rec)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return proc.wait()


def _log_stderr(proc):
    for line in proc.stderr:
        LOG.warning("db2diag: %s", line.decode("utf-8", "replace").rstrip())


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    loki_url = os.environ.get("LOKI_URL")
    if not loki_url:
        LOG.error("LOKI_URL ist nicht gesetzt")
        sys.exit(2)
    cmd = shlex.split(os.environ.get("DB2DIAG_CMD", "db2diag -f -json"))
    sender = LokiSender(loki_url, int(os.environ.get("BATCH_SIZE", "200")),
                        float(os.environ.get("FLUSH_SECONDS", "2")), int(os.environ.get("MAX_BUFFER", "20000")))
    sender.start()
    builder = RecordBuilder(static_labels())
    LOG.info("Sende an %s mit Labels %s", sender.url, builder.static_labels)

    stop_event = threading.Event()

    def _stop(signum, _frame):
        LOG.info("Signal %d - beende", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop_event.is_set():
        try:
            rc = run_command(cmd, builder, sender, stop_event)
            if not stop_event.is_set():
                LOG.warning("db2diag beendet (rc=%s) - Neustart in 10s", rc)
        except FileNotFoundError:
            LOG.error("Befehl nicht gefunden: %s (db2profile geladen?)", cmd[0])
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Fehler: %s", exc)
        stop_event.wait(10)
    sender.stop()
    LOG.info("Beendet. Gesendet: %d, verworfen: %d", sender.sent, sender.dropped)


if __name__ == "__main__":
    main()
