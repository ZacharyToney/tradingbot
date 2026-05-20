from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id     TEXT PRIMARY KEY,
    broker_order_id     TEXT,
    strategy            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,        -- buy|sell
    qty                 REAL NOT NULL,
    order_type          TEXT NOT NULL,        -- market|limit
    limit_price         REAL,
    status              TEXT NOT NULL,        -- new|accepted|filled|cancelled|rejected
    submitted_at_ms     INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL,
    reject_reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol_ts ON orders(symbol, submitted_at_ms);
CREATE INDEX IF NOT EXISTS idx_orders_strategy_ts ON orders(strategy, submitted_at_ms);

CREATE TABLE IF NOT EXISTS fills (
    fill_id             TEXT PRIMARY KEY,
    client_order_id     TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    qty                 REAL NOT NULL,
    price               REAL NOT NULL,
    filled_at_ms        INTEGER NOT NULL,
    FOREIGN KEY (client_order_id) REFERENCES orders(client_order_id)
);

CREATE INDEX IF NOT EXISTS idx_fills_symbol_ts ON fills(symbol, filled_at_ms);

CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    bar_ts_ms           INTEGER NOT NULL,
    target_weight       REAL NOT NULL,
    note                TEXT,
    created_at_ms       INTEGER NOT NULL,
    processed_at_ms     INTEGER,
    UNIQUE(strategy, symbol, bar_ts_ms)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts_ms               INTEGER PRIMARY KEY,
    equity              REAL NOT NULL,
    cash                REAL NOT NULL,
    buying_power        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS daytrades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    closed_at_ms        INTEGER NOT NULL,
    open_client_order_id TEXT NOT NULL,
    close_client_order_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daytrades_ts ON daytrades(closed_at_ms);

CREATE TABLE IF NOT EXISTS starting_equity (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    equity              REAL NOT NULL,
    captured_at_ms      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    qty                 REAL NOT NULL,
    decision            TEXT NOT NULL,        -- pass|reject
    reason              TEXT,
    bar_ts_ms           INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_log_ts ON gate_log(created_at_ms);
"""


_ORDERS_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("quote_bid", "REAL"),
    ("quote_ask", "REAL"),
    ("quote_ts_ms", "INTEGER"),
    ("intended_price", "REAL"),
    ("filled_at_ms", "INTEGER"),
    ("realized_slippage_bps", "REAL"),
    ("fee_qty", "REAL"),
    # Phase 8: track broker-reported filled_qty so partial fills don't over-count in
    # _bot_positions in-flight math.
    ("filled_qty", "REAL"),
)


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add post-v1.0 columns to `orders`.

    SQLite's ALTER TABLE ADD COLUMN is fast and safe (existing rows get NULL), but does
    not support IF NOT EXISTS. We read PRAGMA table_info first and skip columns that
    already exist so this runs cleanly on fresh AND existing databases.
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    for name, col_type in _ORDERS_NEW_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {col_type}")


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    _migrate_add_columns(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
