"""Streamlit dashboard for tradingBot.

Run:
    uv run streamlit run dashboard/app.py

Reads from the bot's SQLite audit log AND the live broker for ground-truth positions.
Manual refresh by default (top-right button) — Streamlit reruns on every interaction.

Sidebar selector switches between Track A (paper, .env) and Track B (chaos, .env.chaos)
so the same dashboard serves both accounts. Cached resources are keyed on env_file
path so broker/DB clients don't bleed between accounts.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pandas as pd
import streamlit as st

from tradingbot.config import REPO_ROOT, load_settings
from tradingbot.db import connect
from tradingbot.execution.broker import AlpacaBroker

st.set_page_config(page_title="tradingBot", page_icon="📈", layout="wide")


# Account choices: label -> env_file path (None = default .env, i.e. Track A).
_ACCOUNTS = {
    "Track A (paper)": None,
    "Track B (chaos)": ".env.chaos",
}


@st.cache_resource
def _settings(env_file: str | None):
    return load_settings(env_file=env_file)


@st.cache_resource
def _broker(_settings_obj, _cache_key: str):
    # _cache_key is just to scope the cache per account; the underlying broker is
    # built from the settings object.
    return AlpacaBroker(_settings_obj)


def _db(env_file: str | None) -> sqlite3.Connection:
    settings = _settings(env_file)
    return connect(settings.db_full_path)


def _query_df(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    rows = conn.execute(sql, params).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def main() -> None:
    # Sidebar account selector — default Track A preserves existing behavior.
    account_label = st.sidebar.selectbox(
        "Account", list(_ACCOUNTS.keys()), index=0, key="account_selector"
    )
    env_file = _ACCOUNTS[account_label]

    # Guard: if Track B is selected but .env.chaos doesn't exist yet, render a
    # helpful message instead of crashing on missing-keys ValidationError.
    if env_file is not None and not (REPO_ROOT / env_file).exists():
        st.title(f"tradingBot — {account_label}")
        st.warning(
            f"`{env_file}` not found at the repo root. Bring Track B up first — see "
            "README → 'Two tracks' → 'Bring up Track B'."
        )
        return

    try:
        settings = _settings(env_file)
    except Exception as e:
        st.title(f"tradingBot — {account_label}")
        st.error(f"failed to load settings from `{env_file or '.env'}`: {e}")
        return

    halt_present = (REPO_ROOT / "HALT").exists()

    st.title(f"tradingBot — {account_label}")
    col1, col2, col3 = st.columns([1, 1, 2])
    col1.metric("Mode", settings.trading_mode.upper())
    col2.metric("HALT", "⚠️ ACTIVE" if halt_present else "✓ clear")
    col3.write(f"UTC now: `{datetime.now(UTC).isoformat(timespec='seconds')}`")

    # --- Top-bar buttons ---
    bar = st.container()
    with bar:
        b1, b2, _b3 = st.columns([1, 1, 6])
        if b1.button("🔄 Refresh", width="stretch"):
            st.rerun()
        halt_label = "🟢 Remove HALT" if halt_present else "🛑 Touch HALT"
        if b2.button(halt_label, width="stretch", type="primary"):
            halt_path = REPO_ROOT / "HALT"
            if halt_present:
                halt_path.unlink(missing_ok=True)
            else:
                halt_path.touch()
            st.rerun()

    st.divider()

    # --- Account snapshot ---
    cache_key = env_file or "_default"
    st.subheader("Account")
    try:
        acc = _broker(settings, cache_key).get_account()
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Equity", f"${acc.equity:,.2f}")
        a2.metric("Cash", f"${acc.cash:,.2f}")
        a3.metric("Buying power", f"${acc.buying_power:,.2f}")
        a4.metric("Day-trades (5d)", str(acc.daytrade_count))
    except Exception as e:
        st.error(f"broker get_account failed: {e}")

    # --- Equity curve ---
    st.subheader("Equity curve (last 30 days)")
    conn = _db(env_file)
    since_ms = int((datetime.now(UTC) - timedelta(days=30)).timestamp() * 1000)
    eq = _query_df(
        conn,
        "SELECT ts_ms, equity FROM equity_snapshots WHERE ts_ms >= ? ORDER BY ts_ms",
        (since_ms,),
    )
    if len(eq) >= 2:
        eq["ts"] = eq["ts_ms"].apply(_ms_to_dt)
        st.line_chart(eq.set_index("ts")["equity"], height=240)
    else:
        st.info("Not enough equity snapshots yet (need ≥2). Run `tb live --paper` to record some.")

    # --- Open positions ---
    pos_cols = st.columns(2)
    with pos_cols[0]:
        st.subheader("Broker positions")
        try:
            positions = _broker(settings, cache_key).get_positions()
            if positions:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "symbol": p.symbol,
                                "qty": p.qty,
                                "entry": p.avg_entry_price,
                                "market_value": p.market_value,
                            }
                            for p in positions
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.info("No open broker positions.")
        except Exception as e:
            st.error(f"get_positions failed: {e}")

    with pos_cols[1]:
        st.subheader("Bot-tracked (audit log)")
        bot_pos = _query_df(
            conn,
            """SELECT o.strategy,
                      f.symbol,
                      SUM(CASE WHEN f.side='buy' THEN f.qty ELSE -f.qty END) AS net_qty
               FROM fills f JOIN orders o ON f.client_order_id = o.client_order_id
               GROUP BY o.strategy, f.symbol HAVING net_qty != 0""",
        )
        if len(bot_pos):
            st.dataframe(bot_pos, hide_index=True, width="stretch")
        else:
            st.info("No bot-tracked positions yet.")

    # --- Today's signals ---
    st.subheader("Today's signals")
    today_start = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
    today_ms = int(today_start.timestamp() * 1000)
    sigs = _query_df(
        conn,
        "SELECT strategy, symbol, target_weight, processed_at_ms, bar_ts_ms "
        "FROM signals WHERE bar_ts_ms >= ? - 86400000 ORDER BY strategy, symbol",
        (today_ms,),
    )
    if len(sigs):
        sigs["bar"] = sigs["bar_ts_ms"].apply(lambda ms: _ms_to_dt(ms).date().isoformat())
        sigs["processed"] = sigs["processed_at_ms"].apply(
            lambda ms: "✓" if pd.notna(ms) else "—"
        )
        st.dataframe(
            sigs[["strategy", "symbol", "bar", "target_weight", "processed"]],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No signals recorded for today's bar yet.")

    # --- Recent fills ---
    st.subheader("Recent fills (last 100)")
    fills = _query_df(
        conn,
        """SELECT f.filled_at_ms, f.symbol, f.side, f.qty, f.price,
                  o.strategy, f.client_order_id
           FROM fills f JOIN orders o ON f.client_order_id = o.client_order_id
           ORDER BY f.filled_at_ms DESC LIMIT 100""",
    )
    if len(fills):
        fills["ts"] = fills["filled_at_ms"].apply(_ms_to_dt)
        st.dataframe(
            fills[["ts", "strategy", "symbol", "side", "qty", "price"]],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No fills yet.")

    # --- Gate decisions ---
    st.subheader("Recent gate decisions (last 20)")
    gates = _query_df(
        conn,
        "SELECT created_at_ms, strategy, symbol, side, qty, decision, reason "
        "FROM gate_log ORDER BY created_at_ms DESC LIMIT 20",
    )
    if len(gates):
        gates["ts"] = gates["created_at_ms"].apply(_ms_to_dt)
        gates["decision"] = gates["decision"].apply(
            lambda d: "✓ pass" if d == "pass" else "✗ reject"
        )
        st.dataframe(
            gates[["ts", "strategy", "symbol", "side", "qty", "decision", "reason"]],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No gate decisions logged yet.")


main()
