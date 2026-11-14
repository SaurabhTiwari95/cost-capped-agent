"""One SQLite file for the index (files, chunks, full-text search) and the spend ledger.

SQLite fits a single-user tool: no server, a file you can delete. WAL mode lets readers and one
writer work at once; a process-wide lock serialises our own writes.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL DEFAULT 0,
    lines      INTEGER NOT NULL,
    summary    TEXT,
    indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_file_idx ON chunks(file_id, start_line);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS spend (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,           -- UTC ISO time
    day           TEXT NOT NULL,           -- UTC date, for the daily cap
    run_id        TEXT NOT NULL,
    purpose       TEXT NOT NULL,           -- answer | summary
    model         TEXT NOT NULL,           -- requested, then the model that served it
    state         TEXT NOT NULL CHECK (state IN ('reserved', 'settled')),
    reserved_usd  TEXT NOT NULL,
    usd           TEXT NOT NULL DEFAULT '0',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS spend_day_idx ON spend(day);
"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # One connection shared by worker threads: every use, reads included, holds this lock.
        # Re-entrant, because reserve() reads the day's total while it holds the lock.
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    # --- spend ledger ---------------------------------------------------------------------------

    def day_committed(self, day: str) -> Decimal:
        """Settled spend plus open reservations for one UTC day."""
        rows = self.query("SELECT state, reserved_usd, usd FROM spend WHERE day = ?", (day,))
        return sum((Decimal(r["usd"] if r["state"] == "settled" else r["reserved_usd"]) for r in rows), Decimal("0"))

    def reserve(
        self, run_id: str, purpose: str, model: str, usd: Decimal, day_cap: Decimal, now: dt.datetime
    ) -> int | None:
        """Atomically: if today's committed spend + usd fits under day_cap, insert a reservation."""
        day = now.date().isoformat()
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")  # no other writer between the check and the insert
            try:
                if self.day_committed(day) + usd > day_cap:
                    self.conn.execute("ROLLBACK")
                    return None
                cur = self.conn.execute(
                    "INSERT INTO spend (at, day, run_id, purpose, model, state, reserved_usd) "
                    "VALUES (?, ?, ?, ?, ?, 'reserved', ?)",
                    (now.isoformat(), day, run_id, purpose, model, str(usd)),
                )
                self.conn.execute("COMMIT")
                return cur.lastrowid
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def settle(self, reservation_id: int, model: str, usd: Decimal, usage) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE spend SET state = 'settled', model = ?, usd = ?, input_tokens = ?, output_tokens = ?,
                   cache_write = ?, cache_read = ? WHERE id = ?""",
                (
                    model,
                    str(usd),
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_write_tokens,
                    usage.cache_read_tokens,
                    reservation_id,
                ),
            )

    def report(self, since_day: str) -> list[sqlite3.Row]:
        return self.query(
            """SELECT day, model, purpose, count(*) AS calls,
                      sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                      sum(cache_write) AS cache_write, sum(cache_read) AS cache_read,
                      group_concat(usd, ',') AS usd_list
               FROM spend WHERE state = 'settled' AND day >= ?
               GROUP BY day, model, purpose ORDER BY day, model, purpose""",
            (since_day,),
        )
