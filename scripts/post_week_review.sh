#!/usr/bin/env bash
# Post-week review for the tradingBot paper-trading run.
#
# Writes a Markdown report to reviews/week_YYYY-MM-DD.md covering bot health,
# trade counts, P&L, and a snapshot of the audit-log SQLite. Designed to be
# fired weekly by `tradingbot-review.timer` (Friday 17:00 ET) but safe to run
# manually any time: `./scripts/post_week_review.sh`.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
REVIEW_DIR="$REPO/reviews"
DATE="$(date +%Y-%m-%d)"
REPORT="$REVIEW_DIR/week_${DATE}.md"
mkdir -p "$REVIEW_DIR"

# Use the editable-installed tb command. Make sure it's on PATH for non-login shells.
export PATH="$HOME/.local/bin:$PATH"

# Build the report in a subshell so a single bad command doesn't abort the rest.
{
    echo "# tradingBot — week review $(date '+%Y-%m-%d %H:%M %Z')"
    echo

    echo "## 1. Repo state (latest commits)"
    echo
    echo '```'
    git -C "$REPO" log --oneline -n 5 2>&1
    echo '```'
    echo

    echo "## 2. Service health"
    echo
    echo '```'
    systemctl --user status tradingbot.service --no-pager 2>&1 | head -20
    echo '```'
    echo

    echo "## 3. Activity (last 7 days)"
    echo
    LINES="$(journalctl --user -u tradingbot --since '7 days ago' --no-pager 2>/dev/null | wc -l)"
    echo "Total log lines: \`$LINES\`"
    echo

    echo "## 4. Errors / circuit-breaker trips (last 7 days)"
    echo
    echo '```'
    ERRORS="$(journalctl --user -u tradingbot --since '7 days ago' --no-pager 2>/dev/null \
        | grep -E 'Error|Traceback|circuit_breaker|tripped|reject' \
        | head -40)"
    if [ -n "$ERRORS" ]; then
        echo "$ERRORS"
    else
        echo "(none — clean run)"
    fi
    echo '```'
    echo

    echo "## 5. Account snapshot"
    echo
    echo '```'
    (cd "$REPO" && tb status 2>&1)
    echo '```'
    echo

    echo "## 6. Reconcile (drift check)"
    echo
    echo '```'
    (cd "$REPO" && tb reconcile 2>&1)
    echo '```'
    echo

    echo "## 7. Audit-log totals"
    echo
    echo "### Per-strategy trade count + net cash flow"
    echo '```'
    sqlite3 "$REPO/tradingbot.db" <<'SQL' 2>&1
.headers on
.mode column
.width 12 8 14
SELECT o.strategy,
       COUNT(*) AS trades,
       ROUND(SUM(CASE WHEN f.side='sell' THEN f.qty*f.price ELSE -f.qty*f.price END), 2) AS net_cashflow
FROM fills f JOIN orders o ON f.client_order_id=o.client_order_id
GROUP BY o.strategy;
SQL
    echo '```'
    echo

    echo "### Gate decision distribution"
    echo '```'
    sqlite3 "$REPO/tradingbot.db" <<'SQL' 2>&1
.headers on
.mode column
.width 10 8
SELECT decision, COUNT(*) AS count FROM gate_log GROUP BY decision;
SQL
    echo '```'
    echo

    echo "### Orders by status"
    echo '```'
    sqlite3 "$REPO/tradingbot.db" <<'SQL' 2>&1
.headers on
.mode column
.width 12 8
SELECT status, COUNT(*) AS count FROM orders GROUP BY status;
SQL
    echo '```'
    echo

    echo "## 8. Audit-log snapshot"
    echo
    SNAPSHOT="$REPO/tradingbot_week_${DATE}.db"
    cp "$REPO/tradingbot.db" "$SNAPSHOT" 2>&1 && echo "Snapshotted to: \`$SNAPSHOT\`"
    echo

    echo "## 9. Walk-forward context (most recent)"
    echo
    WF="$(ls -td "$REPO"/walkforward_reports/donchian_* 2>/dev/null | head -1)"
    if [ -n "$WF" ] && [ -f "$WF/summary.txt" ]; then
        echo "From: \`$WF\`"
        echo '```'
        cat "$WF/summary.txt"
        echo '```'
    else
        echo "No walk-forward reports found yet. Run \`tb walkforward donchian\` to generate."
    fi
    echo

    echo "## 10. Manual next step"
    echo
    echo "Cross-check the per-strategy \`net_cashflow\` numbers above against the Alpaca paper UI:"
    echo
    echo "  https://app.alpaca.markets/paper/dashboard"
    echo
    echo "**Decision matrix** (once you've verified the numbers match):"
    echo
    echo "- **Bot worked, sane P&L** → extend paper to 30 days, then revisit live trading"
    echo "- **Bot worked, P&L poor** → expected per walk-forward (OOS Sharpe ≈ 0.42). Iterate"
    echo "  on strategies on paper. **Don't graduate to live money** based on a poor first week."
    echo "- **Bot misbehaved** (drift detected in §6, errors in §4, or audit log doesn't match"
    echo "  Alpaca UI) → **don't graduate**. Investigate root cause, fix, redo paper trial."
} > "$REPORT" 2>&1

# Desktop notification (Omarchy ships mako / libnotify)
if command -v notify-send >/dev/null 2>&1; then
    notify-send -a tradingbot "Week review ready" "$REPORT" 2>/dev/null || true
fi

echo "$REPORT"
