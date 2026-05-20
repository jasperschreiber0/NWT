module.exports = {
  apps: [
    {
      name: 'master-strategist',
      script: 'master/strategist.py',
      interpreter: 'python3',
      cron_restart: '30 21 * * 1-5',
      autorestart: false,
      watch: false,
      env: { NODE_ENV: 'production' }
    },
    {
      name: 'asx-strategist',
      script: 'asx/strategist.py',
      interpreter: 'python3',
      cron_restart: '0 9 * * 1-5',
      autorestart: false,
      watch: false
    },
    {
      name: 'asx-executor',
      script: 'asx/executor.py',
      interpreter: 'python3',
      cron_restart: '30 9 * * 1-5',
      autorestart: false,
      watch: false
    },
    {
      name: 'ukeu-strategist',
      script: 'ukeu/strategist.py',
      interpreter: 'python3',
      cron_restart: '30 9 * * 1-5',
      autorestart: false,
      watch: false
    },
    {
      name: 'ukeu-executor',
      script: 'ukeu/executor.py',
      interpreter: 'python3',
      cron_restart: '0 10 * * 1-5',
      autorestart: false,
      watch: false
    },
    {
      name: 'us-nightly',
      script: 'us/nightly.py',
      interpreter: 'python3',
      cron_restart: '30 10 * * 1-5',
      autorestart: false,
      watch: false
    },
    {
      name: 'us-trader',
      script: 'us/workspace-northworldtrading/bot/trade_1400_with_brackets.py',
      interpreter: 'python3',
      cron_restart: '5 18 * * 1-5',
      autorestart: false,
      watch: false
    },
    {
      name: 'perf-tracker',
      script: 'performance/tracker.py',
      interpreter: 'python3',
      cron_restart: '0 0 * * 1-5',
      autorestart: false,
      watch: false
    }
  ]
}
