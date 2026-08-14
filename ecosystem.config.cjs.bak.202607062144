// PM2 runs in server local time (AEST = UTC+10).
// All cron_restart times are in AEST. UTC equivalent shown in comments.
// Day-of-week shifts where UTC time crosses midnight into next AEST day.
module.exports = {
  apps: [
    {
      name: 'master-strategist',
      script: 'master/strategist.py',
      interpreter: 'python3',
      cron_restart: '30 7 * * 2-6',   // 07:30 AEST Tue-Sat = 21:30 UTC Mon-Fri (after US close)
      autorestart: false,
      watch: false,
      env: { NODE_ENV: 'production' }
    },
    {
      name: 'asx-strategist',
      script: 'asx/strategist.py',
      interpreter: 'python3',
      cron_restart: '0 19 * * 1-5',   // 19:00 AEST Mon-Fri = 09:00 UTC Mon-Fri
      autorestart: false,
      watch: false
    },
    {
      name: 'asx-executor',
      script: 'asx/executor.py',
      interpreter: 'python3',
      cron_restart: '30 19 * * 1-5',  // 19:30 AEST Mon-Fri = 09:30 UTC Mon-Fri
      autorestart: false,
      watch: false
    },
    {
      name: 'ukeu-strategist',
      script: 'ukeu/strategist.py',
      interpreter: 'python3',
      cron_restart: '30 19 * * 1-5',  // 19:30 AEST Mon-Fri = 09:30 UTC Mon-Fri
      autorestart: false,
      watch: false
    },
    {
      name: 'ukeu-executor',
      script: 'ukeu/executor.py',
      interpreter: 'python3',
      cron_restart: '0 20 * * 1-5',   // 20:00 AEST Mon-Fri = 10:00 UTC Mon-Fri
      autorestart: false,
      watch: false
    },
    {
      name: 'us-nightly',
      script: 'us/nightly.py',
      interpreter: 'python3',
      cron_restart: '30 20 * * 1-5',  // 20:30 AEST Mon-Fri = 10:30 UTC Mon-Fri
      autorestart: false,
      watch: false
    },
    {
      name: 'us-trader',
      script: 'us/workspace-northworldtrading/bot/trade_1400_with_brackets.py',
      interpreter: 'python3',
      cron_restart: '5 4 * * 2-6',    // 04:05 AEST Tue-Sat = 18:05 UTC Mon-Fri (14:05 ET ORB)
      autorestart: false,
      watch: false
    },
    {
      name: 'perf-tracker',
      script: 'performance/tracker.py',
      interpreter: 'python3',
      cron_restart: '0 10 * * 1-5',   // 10:00 AEST Mon-Fri = 00:00 UTC Mon-Fri
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
