# Deploy — systemd user unit

Runs the bot as a systemd user service so it auto-restarts on crash, integrates with
journalctl, and survives reboots (with linger enabled).

## Prerequisites

- `tb` on PATH — install with `uv tool install --editable .` from the repo root.
  Verify: `which tb` should print `~/.local/bin/tb`.
- `.env` with `ALPACA_API_KEY`, `ALPACA_SECRET` populated.
- One-time login session check: `loginctl show-user $(whoami) | grep Linger` should
  print `Linger=yes` once Step 3 below is done.

## Install

```sh
# 1) Drop the unit into your user systemd directory
cp deploy/tradingbot.service ~/.config/systemd/user/

# 2) Reload systemd, enable autostart, and run it now
systemctl --user daemon-reload
systemctl --user enable --now tradingbot.service

# 3) (One-time, requires sudo) Survive reboots and logouts
sudo loginctl enable-linger "$(whoami)"
```

## What `loginctl enable-linger` actually does

Linux user sessions are normally bound to login. When you log out (close GUI session,
SSH disconnect, reboot, etc.) all of your user-level processes get a SIGTERM — including
systemd user services. That's a problem for a long-running trading bot.

`sudo loginctl enable-linger <user>` flips a flag in systemd-logind that says: "this
user's systemd instance and its services should run from boot, independent of login
sessions." After that:

- The bot starts automatically at boot.
- The bot keeps running when you log out of the desktop / close the terminal.
- The bot keeps running across reboots without you needing to log back in first.

It needs sudo because it modifies system-wide login-manager state. It's a one-time
setting and persists forever. Disable later with `sudo loginctl disable-linger <user>`.

## Toggling dry-run ↔ --execute

The committed `tradingbot.service` includes `--execute` (real paper orders). To run a
dry-run instead, edit the `ExecStart` line and drop `--execute`:

```sh
systemctl --user edit --full tradingbot.service   # opens $EDITOR
# Remove the trailing  --execute  from ExecStart
systemctl --user restart tradingbot.service
```

## Watching it run

| | |
|---|---|
| Live log stream | `journalctl --user -u tradingbot -f` |
| Last 200 lines | `journalctl --user -u tradingbot -n 200 --no-pager` |
| Status snapshot | `systemctl --user status tradingbot` |
| On-disk log file (loguru, daily rotation, 30-day retention, zip compressed) | `~/code/tradingBot/logs/tradingbot.log` |
| Audit log (SQLite) | `sqlite3 ~/code/tradingBot/tradingbot.db` |

Note: when running under systemd, the bot's stdout/stderr is captured by the journal
AND the on-disk file is written by loguru. Both exist in parallel — journal is
better for filtering by time, the file is better for grep/archive.

## Stopping / restarting

```sh
# Clean stop (positions preserved; bot exits between ticks)
systemctl --user stop tradingbot

# Fast stop without unloading the service (HALT file)
touch ~/code/tradingBot/HALT
rm   ~/code/tradingBot/HALT  # to let it run again

# Restart after a code change
systemctl --user restart tradingbot
```

The `tb` install is editable, so code changes take effect at the next service restart
without reinstalling.

## Pre-flight checklist (before the first --execute session)

1. `tb status` returns a real account snapshot.
2. `uv run pytest -q` green.
3. Service starts cleanly: `systemctl --user status tradingbot` shows `active (running)`.
4. Logs flow: `journalctl --user -u tradingbot -n 50 --no-pager` shows recent ticks.
5. HALT works: `touch ~/code/tradingBot/HALT` → service exits within ~15s. Then `rm HALT`.
6. Dashboard loads at http://localhost:8501 after `uv run streamlit run dashboard/app.py`.

## Post-week review (automated)

A scheduled review fires every Friday at 17:00 ET. It runs
`scripts/post_week_review.sh`, which writes a Markdown report to
`reviews/week_YYYY-MM-DD.md` and pings via `notify-send`.

### Install the timer

```sh
cp deploy/tradingbot-review.service ~/.config/systemd/user/
cp deploy/tradingbot-review.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tradingbot-review.timer
systemctl --user list-timers tradingbot-review.timer   # confirm next fire
```

### What the report contains

1. Latest commits on the repo
2. Service health (`systemctl --user status tradingbot`)
3. 7-day activity count from journalctl
4. Errors / circuit-breaker trips
5. `tb status` account snapshot
6. `tb reconcile` drift check
7. Per-strategy trade count + net cash flow, gate decision distribution, orders-by-status
8. Snapshotted SQLite DB (`tradingbot_week_YYYY-MM-DD.db`)
9. Most recent walk-forward summary for OOS-expectation comparison
10. Manual cross-check reminder against Alpaca's paper UI + decision matrix

### Run on demand

```sh
./scripts/post_week_review.sh
# Report path is printed to stdout. Reports also written to reviews/.
```

### Manual SQL (if you ever need it directly)

```sh
sqlite3 ~/code/tradingBot/tradingbot.db <<'SQL'
SELECT o.strategy, COUNT(*) AS trades,
       ROUND(SUM(CASE WHEN f.side='sell' THEN f.qty*f.price ELSE -f.qty*f.price END), 2) AS net_proceeds
FROM fills f JOIN orders o ON f.client_order_id=o.client_order_id
GROUP BY o.strategy;

SELECT decision, COUNT(*) FROM gate_log GROUP BY decision;
SQL
```

Cross-check totals against the Alpaca paper dashboard at
https://app.alpaca.markets/paper/dashboard. They must match — if not, that's a
reconciler bug to fix before any live consideration.
