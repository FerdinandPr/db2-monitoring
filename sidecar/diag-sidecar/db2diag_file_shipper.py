#!/usr/bin/env python3
"""
db2diag-file-shipper: Sidecar-Variante des db2diag-Shippers.

Liest die Datei db2diag.log direkt (ohne db2diag-Befehl) aus einem Volume,
das mit dem Db2-Container geteilt wird. Jeder Eintrag wird in ein JSON-Objekt
zerlegt (TIMESTAMP, LEVEL, PID, INSTANCE, NODE, DB, FUNCTION, MESSAGE ...)
und ueber RecordBuilder/LokiSender aus db2diag_shipper.py an Loki geschickt.
Labels und Felder sind damit dieselben wie beim normalen Shipper, die
Dashboards und Alerts funktionieren unveraendert.

Nur Python-Standardbibliothek.

Zusaetzliche Umgebungsvariablen (alle anderen wie in db2diag_shipper.py):
  DB2DIAG_FILE   Pfad oder Glob-Muster, mehrere durch Komma getrennt.
                 Das erste Muster mit Treffer gewinnt, bei mehreren Treffern
                 die zuletzt geaenderte Datei.
  STATE_FILE     merkt sich die Leseposition (Standard /state/position.json)
  START_AT       beginning | end - wo beim ersten Start gelesen wird
                 (Standard beginning)
  POLL_SECONDS   Standard 1
"""

import glob
import json
import logging
import os
import re
import signal
import sys
import threading
import time

from db2diag_shipper import LokiSender, RecordBuilder, static_labels

LOG = logging.getLogger("db2diag-file-shipper")

# Kopfzeile eines Eintrags:
# 2026-11-25-13.09.12.123456+060 I12345E456          LEVEL: Info
_HEADER = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d-\d\d\.\d\d\.\d\d\.\d{1,9}[+-]\d{1,4})\s+(?P<rid>\S+)"
    r"(?:\s+LEVEL\s*:\s*(?P<level>\S+))?")

# Felder im Kopfbereich, mehrere je Zeile moeglich ("PID : 1  TID : 2  PROC : db2sysc 0")
_KEYS = ("PID", "TID", "PROC", "INSTANCE", "NODE", "DB", "APPHDL", "APPID", "UOWID",
         "LAST_ACTID", "AUTHID", "HOSTNAME", "EDUID", "EDUNAME", "FUNCTION", "MESSAGE",
         "CALLED", "RETCODE", "CLIENT_WRKSTNNAME", "CLIENT_ACCTNG", "CLIENT_USERID",
         "CLIENT_APPLNAME", "START", "STOP", "CHANGE", "IMPACT")
_FREE_TEXT = {"FUNCTION", "MESSAGE", "CALLED"}   # Rest der Zeile, nicht weiter zerlegen
_KEY_RE = re.compile(r"(?:^|(?<=\s\s))(%s)\s*:\s?" % "|".join(_KEYS))
_LINE_START = re.compile(r"^(%s)\s*:" % "|".join(_KEYS))


def parse_record(text):
    """Zerlegt einen Eintrag im Textformat in ein dict. RAW enthaelt den ganzen Eintrag."""
    lines = text.rstrip("\n").split("\n")
    obj = {}
    m = _HEADER.match(lines[0])
    if m:
        obj["TIMESTAMP"] = m.group("ts")
        obj["RECORDID"] = m.group("rid")
        if m.group("level"):
            obj["LEVEL"] = m.group("level")
    last_key = None
    for line in lines[1:]:
        if _LINE_START.match(line):
            matches = list(_KEY_RE.finditer(line))
            for i, km in enumerate(matches):
                key = km.group(1)
                free = key in _FREE_TEXT
                end = len(line) if free or i + 1 == len(matches) else matches[i + 1].start()
                obj.setdefault(key, line[km.end():end].strip())
                last_key = key
                if free:
                    break
        elif last_key == "MESSAGE" and line.startswith(" ") and line.strip():
            obj["MESSAGE"] = (obj["MESSAGE"] + " " + line.strip()).strip()
        else:
            last_key = None
    obj["RAW"] = text.rstrip("\n")
    return obj


class Tailer:
    """Liest db2diag.log fortlaufend, erkennt Rotation und merkt sich die Position."""

    def __init__(self, patterns, state_file, start_at):
        self.patterns = [p.strip() for p in patterns.split(",") if p.strip()]
        self.state_file = state_file
        self.start_at = start_at
        self.path = self.fh = None
        self.inode = None
        self.offset = 0
        self.pending = ""      # unvollstaendige Zeile
        self.record = []       # Zeilen des aktuellen Eintrags
        self.carry = []        # fertige Eintraege aus einer rotierten Datei
        self.last_data = time.monotonic()

    # ---------------------------------------------------------- Position
    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def committed_offset(self):
        """Position ohne die Bytes, die noch nicht als Eintrag weitergegeben wurden."""
        unsent = len(self.pending.encode("utf-8"))
        if self.record:
            unsent += len(("\n".join(self.record) + "\n").encode("utf-8"))
        return max(0, self.offset - unsent)

    def save_state(self):
        if not self.path:
            return
        tmp = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"path": self.path, "inode": self.inode, "offset": self.committed_offset()}, fh)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            LOG.warning("Position nicht gespeichert (%s)", exc)

    # ---------------------------------------------------------- Datei
    def find_file(self):
        for pattern in self.patterns:
            hits = [p for p in glob.glob(pattern) if os.path.isfile(p)]
            if hits:
                return max(hits, key=os.path.getmtime)
        return None

    def _open(self, path, from_state):
        st = os.stat(path)
        fh = open(path, "rb")
        state = self._load_state() if from_state else {}
        if state.get("path") == path and state.get("inode") == st.st_ino and state.get("offset", 0) <= st.st_size:
            offset = state["offset"]
            LOG.info("Lese %s ab gespeicherter Position %d", path, offset)
        elif from_state and self.start_at == "end":
            offset = st.st_size
            LOG.info("Lese %s ab Dateiende", path)
        else:
            offset = 0
            LOG.info("Lese %s von Anfang an", path)
        fh.seek(offset)
        if self.fh:
            self.fh.close()
        self.path, self.fh, self.inode, self.offset = path, fh, st.st_ino, offset
        self.pending = ""
        self.record = []

    def ensure_open(self):
        """True, wenn eine Datei offen ist. Erkennt neue/rotierte/gekuerzte Dateien."""
        path = self.find_file()
        if not path:
            return self.fh is not None
        if self.fh is None:
            self._open(path, from_state=True)
            return True
        try:
            st = os.stat(path)
        except OSError:
            return True
        if path != self.path or st.st_ino != self.inode:
            self._drain()  # Rest der alten Datei noch lesen und abschliessen
            self.carry.extend(self._consume_lines())
            if self.pending.strip():
                self.record.append(self.pending.rstrip("\r"))
            if self.record:
                self.carry.append("\n".join(self.record))
            LOG.info("Neue Datei erkannt: %s", path)
            self._open(path, from_state=False)
        elif st.st_size < self.offset:
            LOG.info("%s wurde gekuerzt - lese von vorne", path)
            self._open(path, from_state=False)
        return True

    def _drain(self):
        if self.fh:
            data = self.fh.read()
            if data:
                self.offset += len(data)
                self.pending += data.decode("utf-8", "replace")

    # ---------------------------------------------------------- Eintraege
    def _consume_lines(self):
        out = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            line = line.rstrip("\r")
            if _HEADER.match(line) and self.record:
                out.append("\n".join(self.record))
                self.record = []
            if self.record or line.strip():
                self.record.append(line)
        return out

    def read_records(self, idle_flush=2.0):
        """Liefert fertige Eintraege (Text). Ein Eintrag gilt als fertig, wenn der naechste
        beginnt oder idle_flush Sekunden lang nichts mehr dazukommt."""
        out, self.carry = self.carry, []
        data = self.fh.read(1 << 20) if self.fh else b""
        now = time.monotonic()
        if data:
            self.offset += len(data)
            self.pending += data.decode("utf-8", "replace")
            self.last_data = now
        out.extend(self._consume_lines())
        if self.record and not data and now - self.last_data >= idle_flush:
            out.append("\n".join(self.record))
            self.record = []
        return out, bool(data)

def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    loki_url = os.environ.get("LOKI_URL")
    if not loki_url:
        LOG.error("LOKI_URL ist nicht gesetzt")
        sys.exit(2)
    patterns = os.environ.get("DB2DIAG_FILE", "/database/config/*/sqllib/db2dump/DIAG*/db2diag.log,"
                                              "/database/config/*/sqllib/db2dump/db2diag.log")
    tailer = Tailer(patterns, os.environ.get("STATE_FILE", "/state/position.json"),
                    os.environ.get("START_AT", "beginning").lower())
    poll = float(os.environ.get("POLL_SECONDS", "1"))

    sender = LokiSender(loki_url, int(os.environ.get("BATCH_SIZE", "200")),
                        float(os.environ.get("FLUSH_SECONDS", "2")), int(os.environ.get("MAX_BUFFER", "20000")))
    sender.start()
    builder = RecordBuilder(static_labels())
    LOG.info("Sende an %s mit Labels %s", sender.url, builder.static_labels)
    LOG.info("Suche db2diag.log unter: %s", ", ".join(tailer.patterns))

    stop_event = threading.Event()

    def _stop(signum, _frame):
        LOG.info("Signal %d - beende", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    waiting_logged = False
    last_save = 0.0
    while not stop_event.is_set():
        try:
            if not tailer.ensure_open():
                if not waiting_logged:
                    LOG.info("db2diag.log noch nicht vorhanden - warte (Db2-Setup laeuft?)")
                    waiting_logged = True
                stop_event.wait(5)
                continue
            records, got_data = tailer.read_records()
            for text in records:
                sender.put(builder.from_json(parse_record(text)))
            if time.monotonic() - last_save > 5:
                tailer.save_state()
                last_save = time.monotonic()
            if not got_data:
                stop_event.wait(poll)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Fehler: %s", exc)
            stop_event.wait(5)
    tailer.save_state()
    sender.stop()
    LOG.info("Beendet. Gesendet: %d, verworfen: %d", sender.sent, sender.dropped)


if __name__ == "__main__":
    main()
