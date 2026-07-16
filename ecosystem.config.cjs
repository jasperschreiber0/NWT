// Server timezone is UTC, confirmed via `timedatectl` (see crontab.txt's own
// header comment — "Server runs in UTC ... confirmed via timedatectl").
// All cron_restart values below are literal UTC, matching crontab.txt and
// CLAUDE.md's PM2 Schedule table. This file used to assume AEST (UTC+10)
// for PM2's cron_restart while crontab.txt used literal UTC for the same
// server — two authoritative sources disagreeing about the server's own
// clock, which meant every job here could have been firing ~10 hours off
// from its documented time depending on which assumption was actually
// correct. `time_zone: 'UTC'` makes the interpretation explicit rather than
// implicit in whatever the OS locale happens to be, so this can't silently
// drift again the way it did once already (see git history: "Fix PM2
// cron_restart times: PM2 uses server local time (AEST = UTC+10)" was itself
// a fix for an earlier wrong assumption in the other direction).
module.exports = {
  apps: [
    {
      name: 'master-strategist',
      script: 'master/strategist.py',
      interpreter: 'python3',
      cron_restart: '30 21 * * 1-5',  // 21:30 UTC Mon-Fri (after US close)
      time_zone: 'UTC',
      autorestart: false,
      watch: false,
      env: { NODE_ENV: 'production' }
    },
    {
      name: 'asx-strategist',
      script: 'asx/strategist.py',
      interpreter: 'python3',
      cron_restart: '0 9 * * 1-5',    // 09:00 UTC Mon-Fri
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'asx-executor',
      script: 'asx/executor.py',
      interpreter: 'python3',
      cron_restart: '30 9 * * 1-5',   // 09:30 UTC Mon-Fri
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'ukeu-strategist',
      script: 'ukeu/strategist.py',
      interpreter: 'python3',
      cron_restart: '30 9 * * 1-5',   // 09:30 UTC Mon-Fri
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'ukeu-executor',
      script: 'ukeu/executor.py',
      interpreter: 'python3',
      cron_restart: '0 10 * * 1-5',   // 10:00 UTC Mon-Fri
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'us-nightly',
      script: 'us/nightly.py',
      interpreter: 'python3',
      cron_restart: '30 10 * * 1-5',  // 10:30 UTC Mon-Fri
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'us-trader',
      script: 'us/workspace-northworldtrading/bot/trade_1400_with_brackets.py',
      interpreter: 'python3',
      cron_restart: '5 18 * * 1-5',   // 18:05 UTC Mon-Fri (14:05 ET ORB)
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'us-executor',
      script: 'us/executor.py',
      interpreter: 'python3',
      cron_restart: '10 18 * * 1-5',  // 18:10 UTC Mon-Fri (5 min after us-trader signals)
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'perf-tracker',
      script: 'performance/tracker.py',
      interpreter: 'python3',
      cron_restart: '0 0 * * 1-5',    // 00:00 UTC Mon-Fri
      time_zone: 'UTC',
      autorestart: false,
      watch: false
    },
    {
      name: 'nwt-dashboard',
      script: 'python3',
      args: '-m uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1',
      cwd: '/home/northworld/trading/dashboard',
      autorestart: true,
      watch: false
    }
  ]
}

// NOTE — China bot (china/strategist.py, china/executor.py) intentionally
// has no PM2 entry. It is event-triggered (ADR liquidity confirmation post
// US open), not fixed-time cron, so it runs via crontab.txt's polling window
// (0,30 14-18 UTC Mon-Fri) instead of PM2's cron_restart. Adding PM2 entries
// here too would run two independent supervisors against the same scripts —
// integrity_gate.py's duplicate-runner check would then correctly halt them
// as a conflict. See CLAUDE.md's PM2 Schedule table for the corresponding note.
