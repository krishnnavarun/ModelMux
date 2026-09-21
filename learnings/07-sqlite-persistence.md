# SQLite & Persistence

---

## 1. What it is

**SQLite** is an embedded relational database. Not a server — a **library** that
reads and writes a single file on disk. No process to start, no port, no
credentials.

**`sqlite3`** is Python's standard-library driver for it. No install needed.

---

## 2. How it works

### Embedded, not client-server

| | SQLite | Postgres / MySQL |
|---|---|---|
| Architecture | library in your process | separate server process |
| Setup | none — just a file path | install, configure, credentials |
| Concurrency | one writer at a time | many concurrent writers |
| Network | none | TCP |
| Good for | dev, embedded, single-node, read-heavy | multi-client production |

### ACID

- **Atomicity** — a transaction fully happens or not at all
- **Consistency** — constraints hold before and after
- **Isolation** — concurrent transactions don't see each other's partial work
- **Durability** — committed data survives a crash

SQLite is fully ACID, which surprises people who assume "just a file" means
unreliable.

### Journal modes: rollback vs WAL

**Rollback journal (default):** before modifying, copy original pages to a
journal file. On crash, restore from it. **Readers block writers and vice versa.**

**WAL (Write-Ahead Logging):** append changes to a `-wal` file, periodically fold
them back into the main database ("checkpointing").

```sql
PRAGMA journal_mode=WAL;
```

WAL gives you:
- **Readers don't block writers, writers don't block readers** — the big win
- Usually faster writes

And costs you:
- **Three files instead of one**: `db`, `db-wal`, `db-shm` — they must stay
  mutually consistent
- Doesn't work well on network filesystems
- Needs occasional checkpointing

### Indexes

```sql
CREATE INDEX idx_requests_timestamp ON requests(timestamp);
```

A B-tree keyed on a column. Turns "scan every row" into a logarithmic lookup.
**Cost:** extra storage, and every write must update the index. Index what you
filter and sort by — not everything.

### The Python context-manager trap

```python
with sqlite3.connect(path) as conn:
    conn.execute(...)
# conn is STILL OPEN here.
```

**`sqlite3`'s context manager commits or rolls back the transaction. It does not
close the connection.** This differs from files, sockets, and nearly every other
`with` in Python.

Correct:

```python
from contextlib import closing
with closing(sqlite3.connect(path)) as conn, conn:
    conn.execute(...)
#     │                                     └─ commits
#     └─ closes
```

### Parameterised queries — always

```python
conn.execute("INSERT INTO t (a) VALUES (?)", (value,))          # safe
conn.execute(f"INSERT INTO t (a) VALUES ('{value}')")           # SQL INJECTION
```

The `?` placeholder sends data separately from the statement, so input can never
be parsed as SQL.

---

## 3. How we used it in ModelMux

### One table, no ORM

The spec mandates stdlib `sqlite3` and no ORM. `app/db.py` is the only module
that knows SQL exists.

```sql
CREATE TABLE IF NOT EXISTS requests (
  id                 TEXT PRIMARY KEY,      -- uuid4
  timestamp          TEXT NOT NULL,         -- ISO8601 UTC
  prompt_hash        TEXT NOT NULL,         -- sha256
  prompt_preview     TEXT,                  -- first 80 chars ONLY
  complexity_score   REAL,
  signals_json       TEXT,
  tier, provider, model TEXT,
  cache_hit, escalated, fallback_fired, attempts INTEGER,
  tokens_in, tokens_out INTEGER,
  cost_usd, cost_if_large_usd REAL,
  latency_ms, classify_ms, cache_lookup_ms INTEGER,
  status             TEXT NOT NULL,         -- success | error | cached
  error_message      TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_tier ON requests(tier);
```

Indexes on `timestamp` and `tier` because every dashboard query filters by time
window and groups by tier. **No index on `prompt_hash`** yet — nothing queries it
until the cache lands.

**SQLite has no native boolean.** `cache_hit`, `escalated`, `fallback_fired` are
`INTEGER` 0/1.

### Logging must never fail a request

```python
def log_request(record: dict) -> None:
    try:
        with closing(sqlite3.connect(_db_path, timeout=5.0)) as conn, conn:
            conn.execute(sql, values)
    except Exception as exc:   # noqa: BLE001 — deliberate
        print(f"[db] failed to log request {record.get('id')}: {exc}",
              file=sys.stderr)
```

A blanket `except Exception` is normally a smell. Here it is the requirement:
**logging is observability, not the product.** A full disk should not turn a
working answer into a 500.

`timeout=5.0` is how long to wait if the database is locked by another writer.

### Privacy is a schema decision

We store an 80-char preview plus a sha256 hash, **never the full prompt**. The
request log would otherwise become a permanent transcript of everything anyone
pasted in. Verified by test:

```python
def test_full_prompt_is_never_stored(client, db_path):
    secret = "SENSITIVE" + "y" * 200
    client.post("/v1/chat", json={"prompt": secret})
    row = rows(db_path)[-1]
    assert len(row["prompt_preview"]) == 80
    assert len(row["prompt_hash"]) == 64
```

### The bug: a connection leak that hid for two stages

`db.py` originally used `with sqlite3.connect(...) as conn:` in both functions.
**Every logged request leaked one open connection.** 56 tests passed throughout.

It surfaced sideways: a temp test database wouldn't delete. On Windows an open
connection holds a file handle, which blocks `unlink`. Verified the cause
directly rather than guessing:

```python
with sqlite3.connect(p) as conn:
    conn.execute('CREATE TABLE IF NOT EXISTS t (x)')
conn.execute('SELECT 1')   # -> works fine. Still open.
```

**The lesson: a context manager tells you *something* is scoped, not *what*.**
For any unfamiliar library, check what `__exit__` actually does. The intuitive
reading was wrong in a way that produced no error and no test failure.

### SQLite in a synced folder is a corruption risk

The project lives under OneDrive. A sync client copying `db`, `db-wal` and
`db-shm` independently — or holding a lock mid-write — can produce a set SQLite
considers corrupt.

Fixed by moving only the database:

```yaml
database:
  path: ${LOCALAPPDATA}/ModelMux/modelmux.db
```

**`.gitignore` stops git from committing the files. It does nothing to stop
OneDrive syncing them.** Two different systems, two unrelated ignore mechanisms.
→ DECISIONS.md D7.

---

## 4. Interview questions

**Q: When would you choose SQLite over Postgres?**
Single-node apps, embedded/desktop/mobile, dev environments, read-heavy
workloads, and anything where operational simplicity matters more than
concurrent writes. Switch to Postgres when you need multiple concurrent writers,
network access, replication, or richer types.

**Q: What is WAL mode and what does it trade?**
Write-Ahead Logging appends changes to a separate file instead of copying
original pages out. Readers no longer block writers. Costs: three files that must
stay consistent, poor behaviour on network filesystems, and periodic
checkpointing.

**Q: Explain ACID.**
Atomicity — all or nothing. Consistency — constraints hold across the
transaction. Isolation — concurrent transactions don't observe each other's
partial state. Durability — a commit survives a crash.

**Q: What does `with sqlite3.connect(...)` do?**
It commits on success, rolls back on exception — and **does not close the
connection**. You need `contextlib.closing` as well. *This caused a
connection-per-request leak in my project that no test caught, because it
produced no error — only a file handle that wouldn't release.*

**Q: How do you prevent SQL injection?**
Parameterised queries with `?` placeholders, so values are sent separately from
the statement and can never be parsed as SQL. Never build SQL with string
formatting or f-strings.

**Q: When should you add an index?**
When a column is frequently filtered, joined, or sorted on and the table is large
enough for a scan to hurt. Costs storage and slows writes, since every insert
updates every index. Measure with `EXPLAIN QUERY PLAN` rather than guessing.

**Q: Why store a hash and a preview instead of the full text?**
Privacy — the log would otherwise be a permanent record of user input. The hash
supports dedup and cache keys; the preview supports a readable dashboard.
Neither needs the whole prompt, so we don't keep it.

**Q: Why is SQLite in a Dropbox/OneDrive folder dangerous?**
Sync clients copy files independently and can hold locks mid-write. With WAL
there are three files that must remain mutually consistent — syncing them out of
step can yield a database SQLite reports as corrupt.

**Q: Your logging write fails. Should the request fail?**
Almost never. Logging is observability, not the product — catch, report to
stderr, and return the answer. The exception is regulated audit logging, where
the write *is* part of the contract and failing loudly is correct.

**Q: How would you migrate this to Postgres later?**
Keep all SQL behind one module (as `db.py` does), so the blast radius is one
file. Then: swap the driver to `asyncpg`, change `?` placeholders to `$1`, adjust
types (`INTEGER` booleans → real `BOOLEAN`, `TEXT` timestamps → `TIMESTAMPTZ`),
and add a real connection pool since Postgres connections are expensive.
