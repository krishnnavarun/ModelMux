"""Request logging (SPEC section 6, 8.8).

One table, stdlib sqlite3, no ORM. Two rules from the spec govern this module:

  1. "Nothing is silently dropped" -- every request writes a row, including
     failures.
  2. "A failed database write must never fail the request" -- logging is
     observability, not the product. If it breaks, the caller still gets an
     answer and we complain to stderr.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id                 TEXT PRIMARY KEY,
  timestamp          TEXT NOT NULL,
  prompt_hash        TEXT NOT NULL,
  prompt_preview     TEXT,
  complexity_score   REAL,
  signals_json       TEXT,
  tier               TEXT,
  provider           TEXT,
  model              TEXT,
  cache_hit          INTEGER NOT NULL DEFAULT 0,
  escalated          INTEGER NOT NULL DEFAULT 0,
  fallback_fired     INTEGER NOT NULL DEFAULT 0,
  attempts           INTEGER NOT NULL DEFAULT 1,
  tokens_in          INTEGER,
  tokens_out         INTEGER,
  cost_usd           REAL,
  cost_if_large_usd  REAL,
  latency_ms         INTEGER,
  classify_ms        INTEGER,
  cache_lookup_ms    INTEGER,
  status             TEXT NOT NULL,
  error_message      TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_tier ON requests(tier);
"""

COLUMNS = [
    "id", "timestamp", "prompt_hash", "prompt_preview", "complexity_score",
    "signals_json", "tier", "provider", "model", "cache_hit", "escalated",
    "fallback_fired", "attempts", "tokens_in", "tokens_out", "cost_usd",
    "cost_if_large_usd", "latency_ms", "classify_ms", "cache_lookup_ms",
    "status", "error_message",
]

_db_path: Path | None = None


def init_db(path: Path) -> None:
    """Create the table and indexes if absent. Called once at startup."""
    global _db_path
    _db_path = path
    path.parent.mkdir(parents=True, exist_ok=True)

    # closing() around connect() because sqlite3's own context manager commits
    # the transaction but does NOT close the connection. Using `with
    # sqlite3.connect(...)` alone leaks a connection per call -- which on
    # Windows also keeps an open file handle, blocking deletion of the file.
    with closing(sqlite3.connect(path)) as conn, conn:
        # WAL lets a reader (the dashboard, or you poking at the file) work
        # while a write is in flight, instead of hitting "database is locked".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def log_request(record: dict) -> None:
    """Write one row. Never raises.

    Called from a FastAPI BackgroundTask, so it runs after the response has
    been sent -- the caller never waits on the disk.
    """
    if _db_path is None:
        print("[db] log_request called before init_db", file=sys.stderr)
        return

    try:
        placeholders = ", ".join("?" for _ in COLUMNS)
        sql = f"INSERT INTO requests ({', '.join(COLUMNS)}) VALUES ({placeholders})"
        values = [record.get(column) for column in COLUMNS]

        # closing() closes the connection; the bare `conn` commits it.
        # Both are needed -- see the comment in init_db().
        with closing(sqlite3.connect(_db_path, timeout=5.0)) as conn, conn:
            conn.execute(sql, values)
    except Exception as exc:  # noqa: BLE001 -- deliberate: logging must not fail a request
        print(f"[db] failed to log request {record.get('id')}: {exc}", file=sys.stderr)
