"""감사로그(Audit) — 모든 판정을 남긴다. PoC는 SQLite, 운영 시 Postgres로 교체.

안전 관련 의사결정 지원 시스템은 '왜 그 계획이 추천됐는가'를 사후 재현할 수 있어야 한다.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  flight_id TEXT NOT NULL,
  kind TEXT NOT NULL,          -- DECISION | CABIN_UPDATE | ADVISORY | SIM
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_flight ON audit(flight_id, id);
"""


class AuditStore:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.path = str(Path(data_dir) / "audit.sqlite3")
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def _write(self, ts: str, flight_id: str, kind: str, payload: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO audit(ts, flight_id, kind, payload) VALUES (?,?,?,?)",
                      (ts, flight_id, kind, json.dumps(payload, ensure_ascii=False, default=str)))

    async def log(self, ts: str, flight_id: str, kind: str, payload: dict) -> None:
        await asyncio.to_thread(self._write, ts, flight_id, kind, payload)

    def _read(self, flight_id: str, limit: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, ts, kind, payload FROM audit WHERE flight_id=? "
                             "ORDER BY id DESC LIMIT ?", (flight_id, limit)).fetchall()
        return [{"id": r[0], "ts": r[1], "kind": r[2], "payload": json.loads(r[3])} for r in rows]

    async def read(self, flight_id: str, limit: int = 100) -> list[dict]:
        return await asyncio.to_thread(self._read, flight_id, limit)
