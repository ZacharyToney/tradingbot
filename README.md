# tradingBot

Paper-first algorithmic trading bot on Alpaca (US equities + crypto). Rules-based
technical strategies, hard risk caps, SQLite audit log. Real money is off until
paper trading proves the loop is correct and the strategies have edge.

See the approved plan: `~/.claude/plans/alright-use-my-current-jolly-manatee.md`.

## Quickstart

1. **Sign up for Alpaca paper.** https://alpaca.markets — create a paper account, then
   visit the Paper Trading dashboard and generate an API key/secret.
2. **Configure env.**
   ```sh
   cp .env.example .env
   # edit .env, paste ALPACA_API_KEY and ALPACA_SECRET
   ```
3. **Sync deps.**
   ```sh
   uv sync
   ```
4. **Smoke test the broker.** Confirms bars fetch + account read + a 1-share SPY
   paper order round-trips. Idempotent: rerunning the same day will not duplicate.
   ```sh
   uv run python scripts/smoke_paper_order.py
   ```
5. **Run tests.**
   ```sh
   uv run pytest -q
   ```

## Running the bot

### Live paper-trading loop

Default mode is dry-run (no orders submitted) until you pass `--execute`.

```sh
# Dry-run — computes signals + would-be orders but does NOT submit
uv run tb live --paper

# Submit paper orders for real
uv run tb live --paper --execute

# Pick specific strategies (default: rsi2 only; can also be donchian or both)
uv run tb live --paper --strategies rsi2,donchian
```

The loop polls every 15s. To stop it cleanly, either `touch HALT` or `Ctrl+C`.

### Dashboard

In a second terminal:

```sh
uv run streamlit run dashboard/app.py
```

Opens at http://localhost:8501 — shows account snapshot, equity curve, positions
(broker truth + bot-tracked audit log), today's signals, recent fills, gate decisions,
and a HALT button.

### Backtest

```sh
uv run tb backtest rsi2 --from 2020-01-01 --to 2025-05-01 --symbols SPY,QQQ
uv run tb backtest donchian --from 2020-01-01 --to 2025-05-01 --symbols "BTC/USD,ETH/USD,SOL/USD"
```

Writes `backtest_reports/{strategy}_{utc_ts}/` with summary.txt, trades.csv, equity.csv.

### Walk-forward parameter optimization

Tests whether a strategy's best in-sample params hold out-of-sample. Rolling windows of
(train_months, test_months); for each window the optimizer picks the best params on the
train slice, then evaluates them on the unseen test slice.

```sh
uv run tb walkforward rsi2 --from 2018-01-01 --to 2025-05-01 \
  --symbols SPY,QQQ --train-months 24 --test-months 6 --roll-months 6

uv run tb walkforward donchian --from 2020-01-01 --to 2025-05-01 \
  --symbols "BTC/USD,ETH/USD,SOL/USD" --train-months 24 --test-months 6 --roll-months 6
```

Output in `walkforward_reports/{strategy}_{utc_ts}/windows.csv` + `summary.txt`. If OOS
Sharpe < 50% of IS Sharpe on average, the run flags the strategy as likely overfit.

### Account status / reconcile

```sh
uv run tb status      # account snapshot + open broker positions
uv run tb reconcile   # drift report between bot-tracked positions and broker
uv run tb halt        # create the HALT file
```

## Strategies (v1)

| Strategy | Asset class | Entry | Exit |
| --- | --- | --- | --- |
| `rsi2` mean reversion | Equity ETFs / megacaps | RSI(2) < 10 AND close > SMA(200) | RSI(2) > 70 OR 5 bars elapsed |
| `donchian` breakout | Crypto (BTC/ETH/SOL) | Close > 20-day high (long) | Close < 10-day low (exit) |

## Risk caps (hardcoded in `risk/limits.py`)

- 5% equity per position
- 3 concurrent positions max
- Daily loss limit: -2% → halt for the day
- Total drawdown: -10% → halt + manual restart
- Long-only equities; long/short crypto
- No extended-hours orders
- PDT counter; reject 4th day-trade in 5-day rolling window if equity < $25k
- NTP clock-skew check (>5s off = no trades)
- Equity-drift check (>1% delta from last reconciled = halt)
- Idempotency keys on every order (`client_order_id` = uuid5 of `(strategy, symbol, bar_ts, side)`)
- Broker is source of truth; SQLite is audit log
- **HALT file kill switch:** `touch HALT` → loop exits within one poll cycle

## Verification checklist (before flipping to live money)

- `uv run pytest -q` — all green (currently 115+)
- Out-of-sample backtest: Sharpe > 0.7, max DD < 25%, win rate > 55%
- Walk-forward: OOS Sharpe ≥ 50% of IS Sharpe (no overfit flag)
- Paper session: audit log matches Alpaca web UI byte-for-byte
- Idempotency, HALT file, crash recovery, daily-loss circuit breaker all verified
- Bot has run for ≥5 consecutive trading days on paper with `--execute` before any live consideration

## Architecture

```
data/         broker + parquet cache for OHLCV bars
strategies/   pure-function signal generators (per-symbol)
backtest/     event-driven engine + metrics + walk-forward
risk/         circuit-breaker checks + position sizing + gates composition
execution/    broker wrapper + reconciler + order runner
live/         polling loop wiring data → strategy → gates → reconciler → broker
cli.py        `tb <subcommand>` entry point
dashboard/    Streamlit monitoring UI
```

Critical files (highest blast-radius for bugs):

- `execution/reconciler.py` — diff math; bugs here lose money
- `risk/limits.py` — circuit breakers; bugs here lose more money
- `backtest/engine.py` — no-look-ahead enforcement; bugs here make backtests lie
- `live/loop.py` — the wiring chain
- `strategies/base.py` — interface contract both strategies and backtest depend on
