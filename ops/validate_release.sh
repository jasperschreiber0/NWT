set -euo pipefail
umask 077
testroot=$(mktemp -d /tmp/nwt-reliability-db-XXXXXX)
chown postgres:postgres "$testroot"
chmod 755 "$testroot"
runuser -u postgres -- /usr/lib/postgresql/16/bin/initdb -D "$testroot/data" -A trust --no-locale > "$testroot/init.log"
runuser -u postgres -- /usr/lib/postgresql/16/bin/pg_ctl -D "$testroot/data" -l "$testroot/postgres.log" -o "-k $testroot -p 55449 -c listen_addresses=''" -w start
trap 'runuser -u postgres -- /usr/lib/postgresql/16/bin/pg_ctl -D "$testroot/data" -m fast -w stop' EXIT
export NWT_TEST_DB_DSN="dbname=postgres user=postgres host=$testroot port=55449"
unset NWT_DB_DSN
cd /opt/nwt-reliability-candidate
python3 -m compileall -q execution ops nwt_agents dashboard
python3 -m pytest -q tests_offline nwt_agents/tests execution/tests ukeu/tests ops/test_operations.py research/test_event_observation.py
git diff --check
