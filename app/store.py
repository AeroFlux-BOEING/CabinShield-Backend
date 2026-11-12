"""SQLite persistence (stdlib sqlite3, one connection, WAL). Swap for Postgres by reimplementing `Store`.

Tables
  flights            flight instances (one per flight number + date)
  forecast_versions  every published forecast per route (append-only, version = monotonic int)
  sync_events        everything the tablet uploaded (idempotent on client event id)
  decision_audit     ground re-check of each tablet AI decision
  reports            crew turbulence reports (become future "past similar reports")
  server_events      server-side log (point changes, uplinks, ingests) for the ops console
  wx_observations    live PIREP / AIREP / SIGMET observations kept for a route (NWS Aviation Weather Center)
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
  id TEXT PRIMARY KEY, flight_no TEXT NOT NULL, date TEXT NOT NULL, route_id TEXT NOT NULL, aircraft TEXT NOT NULL,
  dep_local TEXT NOT NULL, dep_utc TEXT NOT NULL, status TEXT NOT NULL, feed TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_versions (
  route_id TEXT NOT NULL, version INTEGER NOT NULL, issued_at TEXT NOT NULL, source TEXT NOT NULL, hazards TEXT NOT NULL,
  PRIMARY KEY (route_id, version));
CREATE TABLE IF NOT EXISTS sync_events (
  id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, device_id TEXT NOT NULL, kind TEXT NOT NULL, t_s REAL NOT NULL,
  at TEXT, payload TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_sync_flight ON sync_events(flight_id, t_s);
CREATE TABLE IF NOT EXISTS decision_audit (
  event_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, t_s REAL NOT NULL, client_pick TEXT NOT NULL, server_pick TEXT NOT NULL,
  agree INTEGER NOT NULL, flags TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id TEXT NOT NULL, route_id TEXT NOT NULL, t_s REAL NOT NULL, d_nm REAL,
  lat REAL, lon REAL, fl REAL, severity INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wx_observations (
  id TEXT NOT NULL, route_id TEXT NOT NULL, kind TEXT NOT NULL, obs_utc TEXT NOT NULL, lat REAL, lon REAL, fl REAL,
  ring INTEGER NOT NULL, d_nm REAL, xtk_nm REAL, raw TEXT, payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (id, route_id));
CREATE INDEX IF NOT EXISTS ix_wx_route ON wx_observations(route_id, obs_utc);
CREATE TABLE IF NOT EXISTS server_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id TEXT, kind TEXT NOT NULL, t_s REAL, msg TEXT NOT NULL, data TEXT, created_at TEXT NOT NULL);
"""


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(x: dt.datetime | dt.date | None) -> str | None:
    return x.isoformat() if x is not None else None


class Store:
    def __init__(self, data_dir: str | None):
        if data_dir in (None, ":memory:"):
            path = ":memory:"
        else:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            path = str(Path(data_dir) / "cabinshield.sqlite3")
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---- flights
    def upsert_flight(self, f: dict) -> None:
        self._exec("""INSERT INTO flights VALUES (:id,:flight_no,:date,:route_id,:aircraft,:dep_local,:dep_utc,:status,:feed,:created_at)
                      ON CONFLICT(id) DO UPDATE SET status=excluded.status, dep_local=excluded.dep_local,
                      dep_utc=excluded.dep_utc, feed=excluded.feed""",
                   {**f, "date": _iso(f["date"]), "dep_utc": _iso(f["dep_utc"]), "created_at": _iso(f["created_at"])})

    def get_flight(self, fid: str) -> dict | None:
        rows = self._all("SELECT * FROM flights WHERE id=?", (fid,))
        return rows[0] if rows else None

    def list_flights(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM flights ORDER BY created_at DESC LIMIT ?", (limit,))

    # ---- forecast
    def latest_forecast(self, route_id: str) -> dict | None:
        rows = self._all("SELECT * FROM forecast_versions WHERE route_id=? ORDER BY version DESC LIMIT 1", (route_id,))
        if not rows:
            return None
        r = rows[0]
        return {**r, "hazards": json.loads(r["hazards"])}

    def add_forecast(self, route_id: str, hazards: list[dict], source: str) -> dict:
        cur = self.latest_forecast(route_id)
        version = (cur["version"] + 1) if cur else 1
        issued = now_utc()
        self._exec("INSERT INTO forecast_versions VALUES (?,?,?,?,?)",
                   (route_id, version, _iso(issued), source, json.dumps(hazards, ensure_ascii=False)))
        return {"route_id": route_id, "version": version, "issued_at": _iso(issued), "source": source, "hazards": hazards}

    # ---- sync
    def add_sync_event(self, flight_id: str, device_id: str, e: dict) -> bool:
        """False when the event id was already stored (idempotent re-upload)."""
        try:
            self._exec("INSERT INTO sync_events VALUES (?,?,?,?,?,?,?,?)",
                       (e["id"], flight_id, device_id, e["kind"], e["t_s"], _iso(e.get("at")),
                        json.dumps(e.get("payload", {}), ensure_ascii=False), _iso(now_utc())))
            return True
        except sqlite3.IntegrityError:
            return False

    def sync_events(self, flight_id: str, kind: str | None = None, limit: int = 200) -> list[dict]:
        q, a = "SELECT * FROM sync_events WHERE flight_id=?", [flight_id]
        if kind:
            q += " AND kind=?"
            a.append(kind)
        rows = self._all(q + " ORDER BY t_s DESC LIMIT ?", (*a, limit))
        return [{**r, "payload": json.loads(r["payload"])} for r in rows]

    def last_sync(self, flight_id: str) -> dict | None:
        rows = self._all("SELECT device_id, MAX(received_at) AS received_at, COUNT(*) AS n FROM sync_events WHERE flight_id=?", (flight_id,))
        return rows[0] if rows and rows[0]["n"] else None

    # ---- audit
    def add_decision_audit(self, event_id: str, flight_id: str, t_s: float, v: dict) -> None:
        self._exec("INSERT OR IGNORE INTO decision_audit VALUES (?,?,?,?,?,?,?,?,?)",
                   (event_id, flight_id, t_s, v["client_pick"], v["server_pick"], int(v["agree"]), json.dumps(v["flags"]),
                    json.dumps(v["server"]), _iso(now_utc())))

    def decision_audit(self, flight_id: str, limit: int = 200) -> list[dict]:
        rows = self._all("SELECT * FROM decision_audit WHERE flight_id=? ORDER BY t_s DESC LIMIT ?", (flight_id, limit))
        return [{**r, "agree": bool(r["agree"]), "flags": json.loads(r["flags"]), "detail": json.loads(r["detail"])} for r in rows]

    def audit_summary(self, flight_id: str) -> dict:
        rows = self._all("SELECT COUNT(*) n, SUM(agree) a, SUM(flags LIKE '%UNSAFE_PICK%') u FROM decision_audit WHERE flight_id=?", (flight_id,))
        r = rows[0]
        n = r["n"] or 0
        return {"decisions": n, "agreement": (r["a"] or 0) / n if n else None, "unsafe_picks": r["u"] or 0}

    # ---- reports
    def add_report(self, flight_id: str, route_id: str, r: dict) -> int:
        cur = self._exec("INSERT INTO reports (flight_id,route_id,t_s,d_nm,lat,lon,fl,severity,note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (flight_id, route_id, r["t_s"], r.get("d_nm"), r.get("lat"), r.get("lon"), r.get("fl"), r["severity"],
                          r.get("note"), _iso(now_utc())))
        return int(cur.lastrowid)

    def reports(self, route_id: str, limit: int = 500) -> list[dict]:
        return self._all("SELECT * FROM reports WHERE route_id=? ORDER BY created_at DESC LIMIT ?", (route_id, limit))

    # ---- live weather
    def add_wx_observations(self, route_id: str, obs: list[dict]) -> int:
        n = 0
        fetched = _iso(now_utc())
        with self._lock:
            for o in obs:
                cur = self._db.execute("INSERT OR IGNORE INTO wx_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (o["id"], route_id, o["kind"], o["obs_utc"], o.get("lat"), o.get("lon"),
                                        o["fl"] if not isinstance(o.get("fl"), list) else o["fl"][1], o["ring"], o.get("d_nm"),
                                        o.get("xtk_nm"), o.get("raw"), json.dumps(o, ensure_ascii=False), fetched))
                n += cur.rowcount
            self._db.commit()
        return n

    def wx_observations(self, route_id: str, since_iso: str | None = None, kind: str | None = None,
                        min_ring: int = 0, limit: int = 2000) -> list[dict]:
        q, a = "SELECT payload FROM wx_observations WHERE route_id=? AND ring>=?", [route_id, min_ring]
        if since_iso:
            q += " AND obs_utc>=?"
            a.append(since_iso)
        if kind:
            q += " AND kind=?"
            a.append(kind)
        rows = self._all(q + " ORDER BY obs_utc DESC LIMIT ?", (*a, limit))
        return [json.loads(r["payload"]) for r in rows]

    # ---- server log
    def add_server_event(self, flight_id: str | None, kind: str, t_s: float | None, msg: dict, data: dict | None = None) -> None:
        self._exec("INSERT INTO server_events (flight_id,kind,t_s,msg,data,created_at) VALUES (?,?,?,?,?,?)",
                   (flight_id, kind, t_s, json.dumps(msg, ensure_ascii=False), json.dumps(data or {}, ensure_ascii=False), _iso(now_utc())))

    def server_events(self, flight_id: str, limit: int = 100) -> list[dict]:
        rows = self._all("SELECT * FROM server_events WHERE flight_id=? ORDER BY id DESC LIMIT ?", (flight_id, limit))
        return [{**r, "msg": json.loads(r["msg"]), "data": json.loads(r["data"])} for r in rows]
