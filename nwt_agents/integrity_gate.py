"""
nwt_agents/integrity_gate.py
Startup Integrity Gate — called by all major scripts before doing anything.
If ANY check fails: log to nwt_system_log, print to stderr, sys.exit(1).

Checks (in order):
  1. No duplicate runners (ps aux)
  2. DB connectivity
  3. Alpaca connectivity (GET /v2/account)
  4. Options chains accessible (GET /v2/options/contracts?underlying_symbols=SPY&limit=1)
  5. Ledger writable (test INSERT + ROLLBACK)
  6. Execution engine live (heartbeat row fresh, or no market hours)
  7. Recon clean (recon_agent --gate exits 0)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import requests


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ["NWT_ALPACA_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["NWT_ALPACA_SECRET_KEY"],
    }


def _alpaca_base() -> str:
    url = os.environ["NWT_ALPACA_BASE_URL"].rstrip("/")
    if url.endswith("/v2"):
        url = url[:-3]
    return url


def _log_critical(message: str, payload: dict = None) -> None:
    """Attempt to log a CRITICAL event to nwt_system_log. Never raises."""
    try:
        conn = psycopg2.connect(os.environ["NWT_DB_DSN"])
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) VALUES (%s, %s, %s, %s)",
                (
                    "CRITICAL",
                    "integrity_gate",
                    message,
                    json.dumps(payload) if payload else None,
                ),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass  # DB might be down — we already have the stderr output


def _fail(message: str, payload: dict = None) -> None:
    print(f"[INTEGRITY GATE FAIL] {message}", file=sys.stderr)
    _log_critical(message, payload)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_no_duplicate_runners(script_name: str) -> None:
    """
    Use ps aux to count processes matching the script name.
    If count > 1, another instance is already running.
    """
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = [
            line for line in result.stdout.splitlines()
            if script_name in line
            and "grep" not in line
            and " >> " not in line  # exclude cron bash wrapper (contains shell redirect)
        ]
        if len(lines) > 1:
            _fail(
                f"Duplicate runner detected for {script_name} — {len(lines)} processes found",
                {"script_name": script_name, "count": len(lines)},
            )
    except Exception as exc:
        _fail(f"Duplicate runner check failed: {exc}")


def _check_db_connectivity() -> None:
    try:
        conn = psycopg2.connect(os.environ["NWT_DB_DSN"])
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
    except Exception as exc:
        _fail(f"DB connectivity check failed: {exc}", {"dsn_set": "NWT_DB_DSN" in os.environ})


def _check_alpaca_connectivity() -> None:
    try:
        url = f"{_alpaca_base()}/v2/account"
        resp = requests.get(url, headers=_alpaca_headers(), timeout=15)
        if resp.status_code != 200:
            _fail(
                f"Alpaca account check failed: HTTP {resp.status_code}",
                {"url": url, "body": resp.text[:500]},
            )
    except requests.RequestException as exc:
        _fail(f"Alpaca connectivity check failed: {exc}")


def _check_options_chains() -> None:
    try:
        url = f"{_alpaca_base()}/v2/options/contracts"
        params = {"underlying_symbols": "SPY", "limit": 1}
        resp = requests.get(url, headers=_alpaca_headers(), params=params, timeout=15)
        if resp.status_code != 200:
            _fail(
                f"Options chain check failed: HTTP {resp.status_code}",
                {"url": url, "body": resp.text[:500]},
            )
        data = resp.json()
        # Accept either a list or dict with 'option_contracts' key
        if isinstance(data, dict):
            contracts = data.get("option_contracts", [])
        else:
            contracts = data
        if not contracts:
            _fail("Options chain check returned empty — no contracts found for SPY")
    except requests.RequestException as exc:
        _fail(f"Options chain check failed: {exc}")


def _check_ledger_writable() -> None:
    try:
        conn = psycopg2.connect(os.environ["NWT_DB_DSN"])
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_portfolio_ledger
                    (bot_source, asset, asset_type, status)
                VALUES ('INTEGRITY_GATE_TEST', 'TEST', 'test', 'open')
                """
            )
            conn.rollback()  # Always rollback — this is only a writability test
        conn.close()
    except Exception as exc:
        _fail(f"Ledger writability check failed: {exc}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _is_market_hours() -> bool:
    et_now = datetime.now(ZoneInfo("America/New_York"))
    return datetime(1, 1, 1, 9, 30).time() <= et_now.time() <= datetime(1, 1, 1, 16, 0).time()


def _check_heartbeat() -> None:
    """Step 6: execution engine heartbeat must be fresh during market hours."""
    if not _is_market_hours():
        return
    try:
        conn = psycopg2.connect(os.environ["NWT_DB_DSN"])
        with conn.cursor() as cur:
            cur.execute("SELECT last_beat FROM nwt_heartbeat WHERE service = 'execution_engine'")
            row = cur.fetchone()
        conn.close()
        if row:
            age = (datetime.now(timezone.utc) - row[0].replace(tzinfo=timezone.utc)).total_seconds()
            if age > 600:  # 10 min stale at gate time is a problem
                _fail(
                    f"Execution engine heartbeat is {age:.0f}s old during market hours",
                    {"last_beat": str(row[0]), "age_seconds": age},
                )
        # No row = engine hasn't run yet, not a failure at startup
    except Exception as exc:
        _fail(f"Heartbeat check failed: {exc}")


def _check_recon() -> None:
    """Step 7: recon_agent --gate must exit 0 (clean ledger vs Alpaca)."""
    try:
        recon_script = Path(__file__).parent / "recon_agent.py"
        result = subprocess.run(
            [sys.executable, str(recon_script), "--gate"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            _fail(
                f"Recon gate failed (exit {result.returncode}): {result.stderr[:500] or result.stdout[:500]}",
                {"returncode": result.returncode},
            )
    except subprocess.TimeoutExpired:
        _fail("Recon gate timed out after 60s")
    except Exception as exc:
        _fail(f"Recon gate check failed: {exc}")


def run_integrity_gate(skip_duplicate_check: bool = False) -> None:
    """
    Run all 7 startup checks.
    Raises SystemExit(1) on the first failure.
    """
    script_name = Path(sys.argv[0]).name if sys.argv else "unknown"

    if not skip_duplicate_check:
        _check_no_duplicate_runners(script_name)

    _check_db_connectivity()
    _check_alpaca_connectivity()
    _check_options_chains()
    _check_ledger_writable()
    _check_heartbeat()
    _check_recon()

    # Log successful gate pass to DB
    try:
        conn = psycopg2.connect(os.environ["NWT_DB_DSN"])
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message) VALUES (%s, %s, %s)",
                ("INFO", "integrity_gate", f"Startup integrity gate passed for {script_name}"),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass

    print(f"[INTEGRITY GATE] All 7 checks passed for {script_name}", flush=True)


if __name__ == "__main__":
    run_integrity_gate(skip_duplicate_check=True)
