"""Schema migrations: ALTER TABLE ADD COLUMN must be idempotent so connect() can
run safely on both fresh and pre-existing databases."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tradingbot.db import _ORDERS_NEW_COLUMNS, connect


def test_connect_adds_new_columns_to_pre_existing_orders_table(tmp_path: Path):
    """Simulate an old database written before the Phase 7 schema additions, then
    verify connect() backfills the new columns."""
    db_path = tmp_path / "old.db"
    # Build a stripped-down orders table that lacks the new columns.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE orders (
            client_order_id     TEXT PRIMARY KEY,
            broker_order_id     TEXT,
            strategy            TEXT NOT NULL,
            symbol              TEXT NOT NULL,
            side                TEXT NOT NULL,
            qty                 REAL NOT NULL,
            order_type          TEXT NOT NULL,
            limit_price         REAL,
            status              TEXT NOT NULL,
            submitted_at_ms     INTEGER NOT NULL,
            updated_at_ms       INTEGER NOT NULL,
            reject_reason       TEXT
        )"""
    )
    conn.commit()
    conn.close()

    # Re-open through the production connect path; migration should fire.
    conn2 = connect(db_path)
    cols = {r["name"] for r in conn2.execute("PRAGMA table_info(orders)").fetchall()}
    for name, _type in _ORDERS_NEW_COLUMNS:
        assert name in cols, f"column {name} missing after migration"


def test_connect_is_idempotent_when_columns_already_exist(tmp_path: Path):
    """Calling connect() twice on the same DB must not raise."""
    db = tmp_path / "idempo.db"
    connect(db).close()
    # Second call must succeed; no "duplicate column" error.
    conn = connect(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    for name, _type in _ORDERS_NEW_COLUMNS:
        assert name in cols
