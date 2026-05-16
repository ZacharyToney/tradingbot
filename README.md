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
6. **Account status.**
   ```sh
   uv run tb status
   ```

## Phases

- **Phase 1 (now):** scaffold + Alpaca connectivity. Smoke script proves end-to-end.
- **Phase 2:** custom event-driven backtest engine + RSI(2) Connors mean reversion + tests.
- **Phase 3:** risk circuit breakers + reconciler + live paper loop (15s polling, HALT file kill switch).
- **Phase 4:** Donchian breakout (crypto), walk-forward with vectorbt, Streamlit dashboard.

## Strategies (v1)

| Strategy | Asset class | Entry | Exit |
| --- | --- | --- | --- |
| RSI(2) mean reversion | Equity ETFs / megacaps | RSI(2) < 10 AND close > SMA(200) | RSI(2) > 70 OR 5 bars elapsed |
| Donchian breakout | Crypto (BTC/ETH/SOL) | Close > 20-day high (long) | Close < 10-day low (exit) |

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
- Idempotency keys on every order (`client_order_id` = uuid5 of `(strategy, symbol, bar_ts)`)
- Broker is source of truth; SQLite is audit log
- **HALT file kill switch:** `touch HALT` → loop exits within one poll cycle

## Verification checklist (Phase 3+)

See "Verification" in the plan file. Key gates before going live with real money:
- Sharpe > 0.7, max DD < 25%, win rate > 55% on out-of-sample backtest
- Audit log matches Alpaca web UI byte-for-byte after a full paper session
- Idempotency, halt file, crash recovery, daily loss circuit breaker all verified
