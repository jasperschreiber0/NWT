set -euo pipefail
umask 077
cd /home/northworld/trading
git diff --exit-code
git diff --cached --exit-code
set -a
source nwt_agents/.env
set +a
(cd nwt_agents && python3 recon_agent.py --gate)
backup=/var/backups/nwt/reliability-20260915
mkdir -p "$backup"
test ! -e "$backup/checkout.tar.gz"
git rev-parse HEAD > "$backup/previous-sha"
crontab -l > "$backup/crontab"
pm2 jlist > "$backup/pm2.json"
cp /root/.pm2/dump.pm2 "$backup/dump.pm2"
tar -czf "$backup/systemd.tar.gz" /etc/systemd/system/nwt-*.service /etc/systemd/system/nwt-*.timer
pg_dump "$NWT_DB_DSN" -Fc -f "$backup/database.dump"
pg_restore --list "$backup/database.dump" > "$backup/database-catalog.txt"
tar -czf "$backup/checkout.tar.gz" --exclude=.git -C /home/northworld trading
gzip -t "$backup/checkout.tar.gz"
printf 'BACKUP_VERIFIED %s\n' "$backup"
