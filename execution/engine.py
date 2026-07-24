"""
execution/engine.py
Execution Engine — lifecycle service.
Zero opinion on whether to trade. Executes only what has been approved.

Each run (every 5 min via cron):
  1. Check no_trade_mode flag — exit if set.
  2. Upsert heartbeat.
  3. Run position monitor — close equity positions at stop/target/max-hold.
  4. Process pending FORCE_CLOSE / CLOSE_REQUEST tickets.
  5. Process pending TRADE_REQUEST tickets (place new orders).
"""

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

from ledger import close_position, get_open_positions, insert_position, log_system_event

_here = Path(__file__).parent
# override=True: the PM2-inherited ambient environment must never shadow this
# service's own .env (same root cause as the Track A bot 401 outage — a stale
# ambient ALPACA_DATA_URL silently beat every bot's correct .env value).
load_dotenv(_here / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("execution_engine")


def _clean_alpaca_base_url(url: str) -> str:
    # Strip trailing slash AND a trailing /v2 (CLAUDE.md gotcha: every call
    # site appends its own /v2/... path, so a misconfigured env var causes a
    # silent double /v2/v2/ -> 404 on every request).
    url = (url or "").rstrip("/")
    if url.lower().endswith("/v2"):
        url = url[:-len("/v2")]
    return url


ALPACA_BASE_URL = _clean_alpaca_base_url(os.environ["ALPACA_BASE_URL"])
ALPACA_DATA_URL = _clean_alpaca_base_url(os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets"))
ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
NWT_DB_DSN = os.environ["NWT_DB_DSN"]
SHARED_DIR = Path(os.environ.get("SHARED_DIR", _here.parent / "shared"))

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json",
}

REQUIRED_FIELDS = {
    "approved", "bot_source", "symbol", "direction",
    "strategy_id", "sized_notional", "asset_type", "time_in_force",
}

POLL_INTERVAL = 3
POLL_MAX = 10
ET_TZ = ZoneInfo("America/New_York")

# P0-1: how old an orphaned CLAIMED ticket decision must be before this run
# will resume it. try_acquire_singleton_lock() already guarantees at most
# one engine instance runs at a time, so any CLAIMED row found at the start
# of a run is, by construction, orphaned by a crashed prior run rather than
# a live concurrent one — this threshold is defense-in-depth against that
# guarantee ever being weakened, not the primary safety mechanism.
CLAIM_STALE_SECONDS = 60

# Aggregate same-direction notional cap (long vs short across all bots/tracks).
# Distinct from master/strategist.py's PER_BOT_WEIGHT_CEILING, which caps a
# single bot's share of total capital — the two are complementary controls
# with similar names, not the same control counted twice.
DIRECTIONAL_CAP_PCT = 0.60

# Synchronous risk backstop — mirrors risk_agent rules. The risk agent's
# 5-minute sweep is authoritative, but its APPROVED decision can be minutes
# stale by the time an order is placed; these flags are re-checked here, in
# the order path, so no order reaches Alpaca after the state has turned.
TRACK_COOLOFF_HOURS = 24

# Track A equity bot_source -> master-directives.json bot_permissions key.
# The options stack (NWT_TRACK_C/D/E) has no bot_permissions entry — it's
# governed by risk_agent's own rules instead, so it's intentionally absent.
BOT_SOURCE_TO_PERMISSIONS_KEY = {
    "US_BOT": "us",
    "EU_BOT": "eu",
    "AUS_BOT": "aus",
    "CHINA_BOT": "china",
}


def _entry_cutoff_utc(now: datetime = None) -> datetime:
    """
    15:30 ET in UTC, fully DST-aware. A fixed UTC hour/minute constant is
    only correct half the year (EDT) — in EST (winter) 15:30 ET is 20:30
    UTC, an hour later, so a fixed constant silently vetoes valid,
    already-approved trades for a third of the year.
    """
    et_now = (now or datetime.now(timezone.utc)).astimezone(ET_TZ)
    cutoff = datetime(et_now.year, et_now.month, et_now.day, 15, 30, tzinfo=ET_TZ)
    return cutoff.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(NWT_DB_DSN)


def try_acquire_singleton_lock(conn, lock_name: str) -> bool:
    """
    P0-2: prevent two instances of this cron-scheduled engine run overlapping
    (e.g. a slow prior run still going when the next 5-minute cron firing
    starts). Postgres SESSION-level advisory lock, held for the lifetime of
    `conn` — no lockfile, no new table; a killed process (OOM, reboot) drops
    the backend connection and Postgres releases the lock automatically.

    Identical to nwt_agents/shared_context.py's try_acquire_singleton_lock —
    duplicated here rather than imported because execution/ is a separate
    package with its own .env and doesn't import from nwt_agents/ anywhere
    else in this file either (see load_master_directives below, which is
    likewise a local copy rather than a cross-package import). Keep both
    implementations in sync if this logic ever changes.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)::bigint)", (lock_name,))
        return bool(cur.fetchone()[0])


def load_master_directives() -> dict:
    with open(SHARED_DIR / "master-directives.json") as f:
        return json.load(f)


def directives_is_stale(directives: dict, now_utc: datetime = None) -> tuple:
    """
    P0-5: master-strategist fires 21:30 UTC and stamps master-directives.json
    with THAT day's date, so the file in force for the next session is always
    dated "yesterday" relative to a same-day check made before the next
    21:30 UTC run — accept today's date or yesterday's, never anything
    older. global_kill_switch is still re-read fresh separately below; this
    is an additional, independent veto for the case where the whole file
    (regime, bot_permissions) is stale because master/strategist.py crashed
    before writing today's update.

    Identical to nwt_agents/shared_context.py's directives_is_stale —
    duplicated here for the same reason load_master_directives above is a
    local copy rather than a cross-package import. Keep both in sync.

    Returns (is_stale: bool, reason: str).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_utc.date()
    d = directives.get("date")
    if not d:
        return True, "master-directives.json has no 'date' field — treating as stale"
    expected_yesterday = (today - timedelta(days=1)).isoformat()
    expected_today = today.isoformat()
    if d not in (expected_yesterday, expected_today):
        return True, (
            f"master-directives.json is stale (date={d}, expected "
            f"{expected_yesterday} or {expected_today})"
        )
    return False, ""


def alpaca_get(path: str) -> dict:
    url = f"{ALPACA_BASE_URL}/v2{path}"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def alpaca_post(path: str, body: dict) -> dict:
    url = f"{ALPACA_BASE_URL}/v2{path}"
    resp = requests.post(url, headers=ALPACA_HEADERS, json=body, timeout=15)
    if not resp.ok:
        logger.error("Alpaca POST %s → %d: %s", path, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def alpaca_delete(path: str) -> dict:
    url = f"{ALPACA_BASE_URL}/v2{path}"
    resp = requests.delete(url, headers=ALPACA_HEADERS, timeout=15)
    if not resp.ok:
        logger.error("Alpaca DELETE %s → %d: %s", path, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def get_current_price(symbol: str) -> float:
    url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    return float(resp.json()["trade"]["p"])


def get_alpaca_account_equity() -> float:
    account = alpaca_get("/account")
    return float(account.get("equity", 97_000))


def get_latest_quote(symbol: str, asset_type: str) -> tuple:
    """
    Latest NBBO (bid, ask) for an option or stock symbol.
    Returns (None, None) on failure — quote capture must never block execution;
    the learning agent falls back to a conservative default spread.
    """
    try:
        if asset_type == "option":
            url = f"{ALPACA_DATA_URL}/v1beta1/options/quotes/latest"
            resp = requests.get(url, headers=ALPACA_HEADERS, params={"symbols": symbol}, timeout=15)
            resp.raise_for_status()
            q = (resp.json().get("quotes") or {}).get(symbol) or {}
        else:
            url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/quotes/latest"
            resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
            resp.raise_for_status()
            q = resp.json().get("quote") or {}
        bid = float(q.get("bp") or 0)
        ask = float(q.get("ap") or 0)
        return (bid if bid > 0 else None, ask if ask > 0 else None)
    except Exception as exc:
        logger.warning("Quote fetch failed for %s (%s): %s", symbol, asset_type, exc)
        return None, None


def get_disabled_tracks(conn) -> set:
    """Tracks placed in cooling-off by the risk agent within the last 24h."""
    disabled = set()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT payload FROM nwt_system_log
            WHERE component = 'risk_agent'
              AND message LIKE 'TRACK_DISABLED%'
              AND created_at > NOW() - INTERVAL '{TRACK_COOLOFF_HOURS} hours'
            """
        )
        rows = cur.fetchall()
    for (payload,) in rows:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if isinstance(payload, dict) and payload.get("track"):
            disabled.add(payload["track"])
    return disabled


def _check_bot_permissions(directives: dict, bot_source: str, sized_notional: float) -> tuple:
    """
    Re-verify master-directives.json's per-bot status/capital_weight/size_cap
    for Track A equity bots, independent of whatever the executor already
    claims. This is the last gate before Alpaca — it must not simply trust
    an upstream "approved: true", because directives can change (or an
    executor bug can slip through) in the minutes between ticket creation
    and this run.
    """
    perm_key = BOT_SOURCE_TO_PERMISSIONS_KEY.get(bot_source)
    if perm_key is None:
        return False, ""  # options stack / unknown source — not governed by bot_permissions

    perms = directives.get("bot_permissions", {}).get(perm_key, {})
    status = perms.get("status", "paused")
    if status not in ("active", "reduced"):
        return True, f"Synchronous veto: bot_permissions.{perm_key}.status={status!r} (not active)"

    capital_weight = float(perms.get("capital_weight", 0.0))
    size_cap = float(perms.get("size_cap", 0.0))
    if capital_weight <= 0 or size_cap <= 0:
        return True, f"Synchronous veto: bot_permissions.{perm_key} capital_weight/size_cap is zero"

    try:
        equity = get_alpaca_account_equity()
    except Exception:
        equity = 97_000.0
    max_notional = equity * capital_weight * size_cap
    if sized_notional > max_notional * 1.05:  # small tolerance for rounding
        return True, (
            f"Synchronous veto: sized_notional {sized_notional:.0f} exceeds "
            f"bot_permissions.{perm_key} cap {max_notional:.0f} "
            f"(capital_weight={capital_weight}, size_cap={size_cap})"
        )
    return False, ""


def synchronous_risk_veto(conn, payload: dict) -> tuple:
    """
    Final gate before an order for a NEW position reaches Alpaca.
    Re-reads master-directives.json fresh and re-checks cooling-off, the
    new-entry cutoff, and per-bot permissions. Returns (vetoed: bool, reason: str).
    Closes (FORCE_CLOSE) bypass this — liquidation is always allowed.
    """
    try:
        directives = load_master_directives()
    except Exception:
        return True, "Synchronous veto: master-directives.json unreadable — NO-TRADE MODE"

    stale, stale_reason = directives_is_stale(directives)
    if stale:
        return True, f"Synchronous veto: {stale_reason}"

    if directives.get("global_kill_switch", False):
        return True, "Synchronous veto: global kill switch active"

    bot_source = payload.get("bot_source", "")

    vetoed, reason = _check_bot_permissions(directives, bot_source, float(payload.get("sized_notional", 0)))
    if vetoed:
        return True, reason

    if bot_source.startswith("NWT_TRACK_"):
        track = bot_source.replace("NWT_TRACK_", "")
        if track in get_disabled_tracks(conn):
            return True, f"Synchronous veto: track {track} in cooling-off period"

    if payload.get("asset_type") == "option" and datetime.now(timezone.utc) >= _entry_cutoff_utc():
        return True, (
            f"Synchronous veto: past new-entry cutoff "
            f"{_entry_cutoff_utc().strftime('%H:%M')} UTC (15:30 ET)"
        )

    return False, ""


def poll_order_until_filled(order_id: str) -> dict:
    for attempt in range(POLL_MAX):
        order = alpaca_get(f"/orders/{order_id}")
        status = order.get("status", "")
        if status == "filled":
            return order
        if status in ("canceled", "expired", "rejected", "done_for_day"):
            logger.warning("Order %s terminal status: %s", order_id, status)
            return order
        logger.info("Order %s status=%s (attempt %d/%d)", order_id, status, attempt + 1, POLL_MAX)
        time.sleep(POLL_INTERVAL)
    return alpaca_get(f"/orders/{order_id}")


def insert_decision(conn, ticket_id: str, decision: str, reasoning: str) -> None:
    """
    P0-1: record this ticket's final EXECUTION_ENGINE decision. Upserts on
    the (ticket_id, decided_by) unique index (migrate_2026_07_execution_
    idempotency.sql) — claim_or_resume_ticket() already inserted a CLAIMED
    row before any Alpaca call, so in the normal path this UPDATEs that same
    row (finalizing the claim) rather than creating a second one. Still
    safe to call without a prior claim (inserts fresh) for any code path
    that doesn't go through the claim/execute flow.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_decisions (ticket_id, decision, reasoning, decided_by)
            VALUES (%s, %s, %s, 'EXECUTION_ENGINE')
            ON CONFLICT (ticket_id, decided_by) DO UPDATE
              SET decision = EXCLUDED.decision, reasoning = EXCLUDED.reasoning
            """,
            (ticket_id, decision, reasoning),
        )
    conn.commit()


def claim_or_resume_ticket(conn, ticket_id: str) -> bool:
    """
    P0-1: atomically claim a ticket before doing anything with a side effect
    (an Alpaca call). Turns the old unsafe pattern —

        ticket found -> Alpaca order placed -> decision recorded

    (a crash between the first two steps left the ticket looking untouched,
    so the next cron cycle retried it and placed a second live order) —
    into:

        ticket claimed -> Alpaca order placed -> result recorded -> claim finalized

    Returns True if this run owns the ticket now and must carry it through
    to an insert_decision() call. Returns False if it must not be touched
    this run: already finalized by a previous run, or (should not be
    reachable given the singleton lock, see CLAIM_STALE_SECONDS) claimed too
    recently to safely assume the claimant crashed.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_decisions (ticket_id, decision, reasoning, decided_by)
            VALUES (%s, 'CLAIMED', 'execution_engine processing started', 'EXECUTION_ENGINE')
            ON CONFLICT (ticket_id, decided_by) DO NOTHING
            RETURNING id
            """,
            (ticket_id,),
        )
        fresh_claim = cur.fetchone() is not None
    conn.commit()
    if fresh_claim:
        return True

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT decision, created_at FROM nwt_ticket_decisions
            WHERE ticket_id = %s AND decided_by = 'EXECUTION_ENGINE'
            """,
            (ticket_id,),
        )
        existing = cur.fetchone()

    if not existing or existing["decision"] != "CLAIMED":
        return False  # already finalized (EXECUTED/FAILED/REJECTED/SKIPPED) — nothing to do

    age = (datetime.now(timezone.utc) - existing["created_at"].replace(tzinfo=timezone.utc)).total_seconds()
    if age < CLAIM_STALE_SECONDS:
        logger.warning("Ticket %s: CLAIMED %.0fs ago, below staleness threshold — not resuming", ticket_id, age)
        return False

    logger.warning("Ticket %s: resuming stale CLAIMED decision (%.0fs old) — likely a crashed prior run",
                    ticket_id, age)
    return True


def client_order_id_for(key: str, kind: str) -> str:
    """Deterministic Alpaca client_order_id for a ticket/position + phase —
    the same logical retry always produces the same id, so a resubmission
    after a crash either gets rejected by Alpaca as a duplicate or, via
    find_or_place_order(), is detected and reused before ever being sent."""
    return f"nwt-{kind}-{key}"[:128]


def alpaca_get_by_client_order_id(client_order_id: str) -> dict | None:
    """Look up an order by client_order_id. None if Alpaca has never seen it."""
    url = f"{ALPACA_BASE_URL}/v2/orders:by_client_order_id"
    resp = requests.get(url, headers=ALPACA_HEADERS, params={"client_order_id": client_order_id}, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def find_or_place_order(client_order_id: str, place_fn) -> dict:
    """
    P0-1 crash recovery (Scenario B): if an order with this client_order_id
    already exists at Alpaca — a prior run submitted it and crashed before
    finishing (ledger write, decision write) — reuse it instead of
    resubmitting. Otherwise place_fn() submits a new order carrying this
    same client_order_id.
    """
    existing = alpaca_get_by_client_order_id(client_order_id)
    if existing is not None:
        logger.warning(
            "client_order_id=%s already exists at Alpaca (order_id=%s, status=%s) — "
            "reusing instead of resubmitting (crash-recovery path)",
            client_order_id, existing.get("id"), existing.get("status"),
        )
        return existing
    return place_fn()


def fetch_pending_tickets(conn) -> list:
    """
    Return approved TRADE_REQUEST tickets this run must (re)process: either
    never touched by EXECUTION_ENGINE, or claimed by a run that crashed
    before finalizing (P0-1 recovery — see claim_or_resume_ticket).
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type = 'TRADE_REQUEST'
              AND t.from_agent IN (
                  'EU_EXECUTOR', 'AUS_EXECUTOR', 'CHINA_EXECUTOR', 'NWT_EXECUTION_AGENT'
              )
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM nwt_ticket_decisions d
                      WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
                  )
                  OR EXISTS (
                      SELECT 1 FROM nwt_ticket_decisions d
                      WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
                        AND d.decision = 'CLAIMED'
                        AND d.created_at < NOW() - (%(stale)s || ' seconds')::interval
                  )
              )
            ORDER BY t.created_at ASC
            """,
            {"stale": CLAIM_STALE_SECONDS},
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_force_close_tickets(conn) -> list:
    """
    Return FORCE_CLOSE / CLOSE_REQUEST tickets this run must (re)process:
    either never touched by EXECUTION_ENGINE, or claimed by a run that
    crashed before finalizing (P0-1 recovery — see claim_or_resume_ticket).
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type IN ('FORCE_CLOSE', 'CLOSE_REQUEST')
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM nwt_ticket_decisions d
                      WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
                  )
                  OR EXISTS (
                      SELECT 1 FROM nwt_ticket_decisions d
                      WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
                        AND d.decision = 'CLAIMED'
                        AND d.created_at < NOW() - (%(stale)s || ' seconds')::interval
                  )
              )
            ORDER BY t.created_at ASC
            """,
            {"stale": CLAIM_STALE_SECONDS},
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_ledger_position(conn, position_id: str):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_portfolio_ledger WHERE position_id = %s",
            (position_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# no_trade_mode check
# ---------------------------------------------------------------------------

def check_no_trade_mode(conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute("SELECT value, reason FROM nwt_system_flags WHERE flag = 'no_trade_mode'")
        row = cur.fetchone()
    if row and row[0]:
        return True, row[1] or "no_trade_mode flag is set"
    return False, ""


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def upsert_heartbeat(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_heartbeat (service, last_beat, status)
            VALUES ('execution_engine', NOW(), 'ok')
            ON CONFLICT (service) DO UPDATE SET last_beat=NOW(), status='ok'
            """
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Directional cap
# ---------------------------------------------------------------------------

def check_directional_cap(conn, direction: str, incoming_notional: float) -> tuple:
    try:
        equity = get_alpaca_account_equity()
    except Exception:
        equity = 97_000.0

    cap = equity * DIRECTIONAL_CAP_PCT
    ledger_direction = "long" if direction in ("long", "buy") else "short"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(notional_risk), 0)
            FROM nwt_portfolio_ledger
            WHERE status = 'open' AND direction = %s
            """,
            (ledger_direction,),
        )
        existing = float(cur.fetchone()[0])

    total = existing + incoming_notional
    return total > cap, total, cap


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def compute_qty_from_notional(sized_notional: float, price: float) -> int:
    return max(int(sized_notional / price), 1)


def place_equity_order(payload: dict, client_order_id: str) -> dict:
    symbol = payload["symbol"]
    sized_notional = float(payload["sized_notional"])
    time_in_force = payload["time_in_force"]
    direction = payload["direction"]

    price = get_current_price(symbol)
    qty = compute_qty_from_notional(sized_notional, price)
    side = "buy" if direction == "long" else "sell"

    order_body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
    }
    logger.info("Placing equity order: %s %s x%d (notional=%.2f) client_order_id=%s",
                side, symbol, qty, sized_notional, client_order_id)
    return alpaca_post("/orders", order_body)


def place_options_order(payload: dict, client_order_id: str) -> dict:
    """
    Single-leg option entries are ALWAYS buy-to-open — a bearish thesis buys
    a put, it never sells a call. Sell-to-open is only reachable inside a
    multi-leg (defined-risk) structure (payload["legs"]), submitted as one
    Alpaca mleg order — this is the only path that can hold a short leg, and
    it is always paired with a long leg that bounds the risk.
    """
    qty = int(payload.get("qty", 1))
    time_in_force = payload["time_in_force"]
    legs = payload.get("legs") or []

    if legs:
        order_body = {
            "order_class": "mleg",
            "qty": str(qty),
            "type": "market",
            "time_in_force": time_in_force,
            "client_order_id": client_order_id,
            "legs": [
                {
                    "symbol": leg["option_symbol"],
                    "ratio_qty": "1",
                    "side": leg["side"],
                    "position_intent": f"{leg['side']}_to_open",
                }
                for leg in legs
            ],
        }
        logger.info("Placing mleg options order: %d legs x%d (%s) client_order_id=%s",
                    len(legs), qty, ", ".join(f"{l['side']} {l['option_symbol']}" for l in legs),
                    client_order_id)
        return alpaca_post("/orders", order_body)

    option_symbol = payload["option_symbol"]
    order_body = {
        "symbol": option_symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": time_in_force,
        "order_class": "simple",
        "client_order_id": client_order_id,
    }
    logger.info("Placing options order: buy %s x%d client_order_id=%s", option_symbol, qty, client_order_id)
    return alpaca_post("/orders", order_body)


def place_close_order(symbol: str, qty: int, asset_type: str, client_order_id: str, side: str = "sell") -> dict:
    """
    side defaults to "sell" (closing a long position — true for equity and
    every single-leg option position). A short option leg (only reachable
    inside a multi-leg spread) must be closed with side="buy" instead —
    callers closing a specific ledger position must pass the side that
    matches that position's own direction, not assume "sell".
    """
    order_body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }
    if asset_type == "option":
        order_body["order_class"] = "simple"
    logger.info("Placing close order: %s %s x%d client_order_id=%s", side, symbol, qty, client_order_id)
    return alpaca_post("/orders", order_body)


# ---------------------------------------------------------------------------
# pnl_adjusted computation
# ---------------------------------------------------------------------------

def compute_pnl_adjusted(asset_type: str, pnl: float, entry_price: float,
                          exit_price: float, qty: int = 1,
                          bid_ask_spread: float = 0.0) -> tuple:
    """
    Returns (pnl_adjusted, slippage_model).
    Options: haircut 0.5 × spread × 100 × qty per side (entry + exit).
    Equity: haircut 1bp per side.
    """
    if asset_type == "option":
        spread = bid_ask_spread if bid_ask_spread > 0 else max(entry_price * 0.02, 0.05)
        haircut = 0.5 * spread * 100 * qty * 2
        return pnl - haircut, "half_spread_v1"
    else:
        notional = entry_price * qty
        haircut = notional * 0.0001 * 2
        return pnl - haircut, "equity_1bp_v1"


def write_trade_outcome(conn, position: dict, fill_price: float,
                        exit_reason: str, strategy_id: str = None) -> None:
    """Write a completed nwt_trade_outcomes row. Called on options position close."""
    entry_price = float(position.get("entry_price") or 0)
    notional = float(position.get("notional_risk") or 0)
    qty = max(int(round(notional / (entry_price * 100))) if entry_price > 0 else 1, 1)

    pnl = (fill_price - entry_price) * qty * 100
    if position.get("direction") == "short":
        pnl = -pnl

    pnl_adj, slippage_model = compute_pnl_adjusted(
        "option", pnl, entry_price, fill_price, qty
    )

    entry_time = position.get("entry_time")
    entry_dt = entry_time if isinstance(entry_time, datetime) else datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_trade_outcomes (
              strategy_id, symbol, direction,
              entry_price, entry_time, exit_price, exit_time,
              pnl, pnl_pct, pnl_adjusted, slippage_model,
              position_id, closed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING
            """,
            (
                strategy_id or position.get("bot_source", "UNKNOWN"),
                position.get("asset", ""),
                position.get("direction", ""),
                entry_price, entry_dt, fill_price,
                round(pnl, 4),
                round(pnl / notional, 6) if notional > 0 else 0,
                round(pnl_adj, 4),
                slippage_model,
                str(position.get("position_id", "")),
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Position monitor (equity multi-day exits)
# ---------------------------------------------------------------------------

def run_equity_position_monitor(conn) -> None:
    positions = get_open_positions(conn)
    equity_positions = [p for p in positions if p.get("asset_type") == "equity"
                        and p.get("bot_source") != "UNATTRIBUTED"]

    if not equity_positions:
        return

    for pos in equity_positions:
        symbol = pos.get("asset", "")
        position_id = str(pos.get("position_id", ""))
        entry_price = float(pos.get("entry_price") or 0)
        notional = float(pos.get("notional_risk") or 0)
        direction = pos.get("direction", "long")
        strategy_id = pos.get("bot_source", "")

        if entry_price <= 0 or not symbol:
            continue

        try:
            current_price = get_current_price(symbol)
        except Exception as exc:
            logger.warning("Position monitor: cannot fetch price for %s: %s", symbol, exc)
            continue

        pnl_pct = (current_price - entry_price) / entry_price
        if direction == "short":
            pnl_pct = -pnl_pct

        stop_pct = -0.015
        target_pct = 0.025
        max_hold_days = 20

        # Prefer the per-trade stop_pct/target_pct the Brain/executor actually
        # sent with this ticket (persisted on the ledger row at fill time) —
        # these used to be accepted into the Brain->Execution payload and then
        # silently discarded in favor of the genome/hardcoded default below.
        ledger_stop = pos.get("stop_pct")
        ledger_target = pos.get("target_pct")
        if ledger_stop is not None and ledger_target is not None:
            stop_pct = -abs(float(ledger_stop))
            target_pct = float(ledger_target)
        else:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM nwt_strategy_genome WHERE strategy_id = %s AND active = TRUE",
                        (strategy_id,),
                    )
                    genome = cur.fetchone()
                if genome:
                    stop_pct = -abs(float(genome["stop_loss_pct"]))
                    target_pct = float(genome["profit_target_pct"])
            except Exception:
                pass

        entry_time = pos.get("entry_time")
        if entry_time:
            age_days = (datetime.now(timezone.utc) - entry_time.replace(tzinfo=timezone.utc)).days
            if age_days >= max_hold_days:
                _close_equity_position(conn, pos, current_price, position_id, symbol,
                                       notional, entry_price, "max_hold")
                continue

        exit_reason = None
        if pnl_pct <= stop_pct:
            exit_reason = "stop"
        elif pnl_pct >= target_pct:
            exit_reason = "target"

        if exit_reason:
            _close_equity_position(conn, pos, current_price, position_id, symbol,
                                   notional, entry_price, exit_reason)


def _close_equity_position(conn, pos, current_price, position_id, symbol,
                            notional, entry_price, exit_reason) -> None:
    try:
        qty = compute_qty_from_notional(notional, entry_price)
        # P0-1: this monitor scans open ledger positions every cycle rather
        # than consuming a ticket, so there's no claim to make — but the
        # same crash-recovery gap exists for the Alpaca call itself (fill
        # succeeds, close_position() write crashes, next cycle still sees
        # the position 'open' and would otherwise resubmit). Keyed on
        # position_id (stable per ledger row) rather than a ticket_id.
        client_order_id = client_order_id_for(position_id, "eqmon")
        order = find_or_place_order(
            client_order_id,
            lambda: place_close_order(symbol, qty, "equity", client_order_id),
        )
        filled = poll_order_until_filled(order["id"])
        fill_price = float(filled.get("filled_avg_price") or current_price)
        slippage = abs(fill_price - current_price) / current_price if current_price > 0 else 0.0
        close_position(conn, position_id, fill_price, slippage, exit_reason)
        log_system_event(conn, "INFO", "execution_engine",
                         f"Closed equity {symbol} reason={exit_reason} fill={fill_price:.4f}",
                         {"position_id": position_id, "exit_reason": exit_reason,
                          "fill_price": fill_price})
        logger.info("Closed equity %s at %.4f reason=%s", symbol, fill_price, exit_reason)
    except Exception as exc:
        logger.error("Failed to close equity position %s: %s", position_id, exc)
        log_system_event(conn, "ERROR", "execution_engine",
                         f"Equity close failed for {symbol}: {exc}",
                         {"position_id": position_id})


# ---------------------------------------------------------------------------
# FORCE_CLOSE / CLOSE_REQUEST processing
# ---------------------------------------------------------------------------

def process_close_ticket(conn, ticket: dict) -> None:
    """
    Handle CLOSE_REQUEST tickets. The close side must match the position's
    own direction — a long position (every single-leg option, or the long
    leg of a spread) is closed by selling; a short leg (only reachable
    inside a spread) is closed by buying it back. Look up the ledger
    position first so this can never default to "sell" against a short.
    """
    ticket_id = str(ticket["ticket_id"])
    if not claim_or_resume_ticket(conn, ticket_id):
        logger.info("Ticket %s: not claimed this run — skipping", ticket_id)
        return

    payload = ticket.get("payload") or {}
    symbol = payload.get("option_symbol") or payload.get("symbol", "")
    position_id = payload.get("position_id")
    exit_reason = payload.get("exit_reason", "hard_close")
    asset_type = payload.get("asset_type", "option")
    qty = int(payload.get("qty", 1))

    pos_direction = payload.get("direction", "long")
    if position_id:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT direction FROM nwt_portfolio_ledger WHERE position_id = %s",
                        (position_id,))
            row = cur.fetchone()
        if row and row.get("direction"):
            pos_direction = row["direction"]
    close_side = "buy" if pos_direction == "short" else "sell"

    client_order_id = client_order_id_for(ticket_id, "close")
    try:
        order = find_or_place_order(
            client_order_id,
            lambda: place_close_order(symbol, qty, asset_type, client_order_id, side=close_side),
        )
        filled = poll_order_until_filled(order["id"])
        fill_price = float(filled.get("filled_avg_price") or 0)
        fill_status = filled.get("status", "")

        if fill_status != "filled" or fill_price <= 0:
            insert_decision(conn, ticket_id, "FAILED",
                            f"Close order not filled — status={fill_status}")
            return

        slippage = 0.0
        if position_id:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id = %s",
                            (position_id,))
                pos = cur.fetchone()
            if pos:
                pos = dict(pos)
                close_position(conn, position_id, fill_price, slippage, exit_reason)
                if asset_type == "option":
                    write_trade_outcome(conn, pos, fill_price, exit_reason,
                                        payload.get("strategy_id"))

        insert_decision(conn, ticket_id, "EXECUTED",
                        f"Closed {symbol} at {fill_price:.4f} reason={exit_reason}")
        log_system_event(conn, "INFO", "execution_engine",
                         f"Close executed: {symbol} at {fill_price:.4f}",
                         {"ticket_id": ticket_id, "exit_reason": exit_reason,
                          "position_id": position_id})
    except Exception as exc:
        reason = f"Close failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})


def process_force_close(conn, ticket: dict) -> None:
    """
    Liquidate a ledger position on RISK_AGENT FORCE_CLOSE instruction.
    Uses Alpaca's close-position endpoint (whole position), polls the fill,
    and closes the ledger row with exit price + exit NBBO.
    """
    ticket_id = str(ticket["ticket_id"])
    if not claim_or_resume_ticket(conn, ticket_id):
        logger.info("Ticket %s: not claimed this run — skipping", ticket_id)
        return

    payload = ticket.get("payload") or {}
    position_id = payload.get("position_id", "")
    asset = payload.get("symbol") or payload.get("option_symbol", "")

    position = get_ledger_position(conn, position_id) if position_id else None
    if position is None:
        reason = f"FORCE_CLOSE: ledger position {position_id} not found"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    if position.get("status") == "closed":
        # P0-1 crash recovery (Scenario D): a resumed FORCE_CLOSE whose
        # prior attempt already liquidated at Alpaca and wrote the ledger
        # close lands here and stops — no second liquidation attempt.
        reason = f"FORCE_CLOSE: position {position_id} already closed"
        logger.info("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "SKIPPED", reason)
        return

    asset = position.get("asset") or asset
    asset_type = position.get("asset_type", "option")

    exit_bid, exit_ask = get_latest_quote(asset, asset_type)
    expected_price = (exit_bid + exit_ask) / 2.0 if (exit_bid and exit_ask) else None

    try:
        # Alpaca's position-liquidation endpoint (DELETE /v2/positions/{..})
        # does not accept a client_order_id — unlike every other order path
        # in this file, that idempotency mechanism isn't available here.
        # Crash safety instead comes from the status=='closed' check above
        # (resumed after a fully-completed prior attempt: skips) plus
        # Alpaca's own behaviour when there is genuinely nothing left to
        # liquidate (resumed after Alpaca-succeeded-but-ledger-write-crashed:
        # this call 404s — caught below as FAILED, not a second live order).
        order = alpaca_delete(f"/positions/{asset}")
    except Exception as exc:
        reason = f"FORCE_CLOSE: liquidation order failed for {asset}: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    alpaca_order_id = order.get("id", "")
    try:
        filled_order = poll_order_until_filled(alpaca_order_id) if alpaca_order_id else order
    except Exception as exc:
        reason = f"FORCE_CLOSE: order poll failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None
    if fill_price is None:
        reason = f"FORCE_CLOSE: no fill price — status={filled_order.get('status')}"
        logger.warning("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {
            "ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id,
        })
        return

    slippage = abs(fill_price - expected_price) / expected_price if expected_price and expected_price > 0 else 0.0
    close_position(conn, position_id, fill_price, slippage, "hard_close",
                   exit_bid=exit_bid, exit_ask=exit_ask)
    reasoning = (
        f"FORCE_CLOSE filled at {fill_price:.4f}, slippage={slippage:.4f}, "
        f"position_id={position_id}, alpaca_order_id={alpaca_order_id}"
    )
    insert_decision(conn, ticket_id, "EXECUTED", reasoning)
    log_system_event(conn, "INFO", "execution_engine",
                     f"Force-closed {asset} ({asset_type})",
                     {"ticket_id": ticket_id, "position_id": position_id,
                      "alpaca_order_id": alpaca_order_id, "fill_price": fill_price,
                      "slippage": slippage})
    logger.info("Ticket %s FORCE_CLOSE EXECUTED: position=%s fill=%.4f", ticket_id, position_id, fill_price)


# ---------------------------------------------------------------------------
# Trade ticket processing
# ---------------------------------------------------------------------------

def insert_spread_ledger_rows(conn, ticket_id: str, payload: dict,
                               filled_order: dict, alpaca_order_id: str) -> str:
    """
    One ledger row PER LEG of a filled mleg order, tied by spread_group_id.
    Recon matches Alpaca positions per contract, so legs must be individual
    rows; the monitor values/closes the structure as a unit via the group id.
    Returns the spread_group_id.

    P0-1: if a prior run already inserted some legs for this alpaca_order_id
    before crashing, reuse ITS spread_group_id instead of minting a new one
    — otherwise a resumed insert would split one spread's legs across two
    different group ids. insert_position() below is idempotent per
    (alpaca_order_id, asset), so already-inserted legs are safely skipped
    and only the missing ones get written.
    """
    qty = int(payload.get("qty", 1))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT spread_group_id FROM nwt_portfolio_ledger WHERE alpaca_order_id = %s LIMIT 1",
            (alpaca_order_id,),
        )
        existing_group = cur.fetchone()
    spread_group_id = str(existing_group["spread_group_id"]) if existing_group else str(uuid.uuid4())
    filled_legs = {l.get("symbol"): l for l in (filled_order.get("legs") or [])}
    position_ids = []

    for leg in payload["legs"]:
        leg_symbol = leg["option_symbol"]
        fl = filled_legs.get(leg_symbol, {})
        leg_fill = fl.get("filled_avg_price")
        leg_fill = float(leg_fill) if leg_fill else None

        leg_bid, leg_ask = get_latest_quote(leg_symbol, "option")
        if leg_fill is None:
            # Leg fill missing from the order response — fall back to quote mid
            if leg_bid and leg_ask:
                leg_fill = (leg_bid + leg_ask) / 2.0
            else:
                leg_fill = 0.0

        side = leg["side"]
        leg_direction = "long" if side == "buy" else "short"
        base_delta = 0.5 if leg.get("option_type", "call") == "call" else -0.5
        delta_exposure = base_delta if leg_direction == "long" else -base_delta

        ledger_data = {
            "bot_source": payload["bot_source"],
            "strategy_id": payload.get("strategy_id"),
            "asset": leg_symbol,
            "asset_type": "option",
            "direction": leg_direction,
            "delta_exposure": delta_exposure,
            "notional_risk": abs(leg_fill) * 100 * qty,
            "qty": qty,
            "entry_price": leg_fill,
            "entry_time": datetime.now(timezone.utc),
            "entry_bid": leg_bid,
            "entry_ask": leg_ask,
            "alpaca_order_id": alpaca_order_id,
            "stop_pct": payload.get("stop_pct"),
            "target_pct": payload.get("target_pct"),
            "spread_group_id": spread_group_id,
        }
        position_ids.append(insert_position(conn, ledger_data))

    reasoning = (f"mleg filled — {len(position_ids)} legs, "
                 f"spread_group_id={spread_group_id}, alpaca_order_id={alpaca_order_id}")
    insert_decision(conn, ticket_id, "EXECUTED", reasoning)
    log_system_event(conn, "INFO", "execution_engine",
                     f"Executed spread {payload.get('strategy_type', '')} on {payload.get('symbol', '')}",
                     {"ticket_id": ticket_id, "spread_group_id": spread_group_id,
                      "position_ids": position_ids, "alpaca_order_id": alpaca_order_id,
                      "strategy_id": payload.get("strategy_id")})
    logger.info("Ticket %s EXECUTED (spread): group=%s legs=%d",
                ticket_id, spread_group_id, len(position_ids))
    return spread_group_id


def process_ticket(conn, ticket: dict, directives: dict) -> None:
    ticket_id = str(ticket["ticket_id"])
    if not claim_or_resume_ticket(conn, ticket_id):
        logger.info("Ticket %s: not claimed this run — skipping", ticket_id)
        return

    payload = ticket.get("payload") or {}

    missing = REQUIRED_FIELDS - set(payload.keys())
    if missing:
        reason = f"Missing required fields: {sorted(missing)}"
        logger.warning("Ticket %s rejected: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    if not payload.get("approved", False):
        reason = payload.get("reasoning", "approved=False in payload")
        insert_decision(conn, ticket_id, "REJECTED", reason)
        return

    # Synchronous risk gate — re-reads directives fresh at order time
    vetoed, veto_reason = synchronous_risk_veto(conn, payload)
    if vetoed:
        logger.warning("Ticket %s rejected: %s", ticket_id, veto_reason)
        insert_decision(conn, ticket_id, "REJECTED", veto_reason)
        log_system_event(conn, "WARNING", "execution_engine", veto_reason, {"ticket_id": ticket_id})
        return

    # Directional cap check
    direction = payload.get("direction", "long")
    sized_notional = float(payload.get("sized_notional", 0))
    cap_exceeded, total_exposure, cap = check_directional_cap(conn, direction, sized_notional)
    if cap_exceeded:
        reason = (f"Directional cap exceeded: {direction} exposure {total_exposure:.0f} "
                  f"> {cap:.0f} (60% of equity)")
        logger.warning("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason,
                         {"ticket_id": ticket_id, "type": "directional_cap_reject",
                          "total_exposure": total_exposure, "cap": cap})
        return

    asset_type = payload["asset_type"]
    symbol = payload["symbol"]
    legs = payload.get("legs") or []
    expected_price = None
    entry_bid = entry_ask = None
    client_order_id = client_order_id_for(ticket_id, "trade")

    try:
        if asset_type == "equity":
            entry_bid, entry_ask = get_latest_quote(symbol, "equity")
            order = find_or_place_order(client_order_id, lambda: place_equity_order(payload, client_order_id))
            expected_price = get_current_price(symbol)
        elif asset_type == "option":
            if not legs:
                option_symbol = payload.get("option_symbol", symbol)
                entry_bid, entry_ask = get_latest_quote(option_symbol, "option")
                if entry_bid and entry_ask:
                    expected_price = (entry_bid + entry_ask) / 2.0
            order = find_or_place_order(client_order_id, lambda: place_options_order(payload, client_order_id))
        else:
            reason = f"Unknown asset_type: {asset_type}"
            insert_decision(conn, ticket_id, "FAILED", reason)
            return
    except Exception as exc:
        reason = f"Order placement failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    alpaca_order_id = order["id"]

    try:
        filled_order = poll_order_until_filled(alpaca_order_id)
    except Exception as exc:
        reason = f"Order poll failed: {exc}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    fill_status = filled_order.get("status", "")
    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None

    # mleg orders report per-leg fills; the top-level price is the net debit/
    # credit and may legitimately be absent — status alone decides for spreads
    if fill_status != "filled" or (fill_price is None and not legs):
        reason = f"Order did not fill — final status={fill_status}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason,
                         {"ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id})
        return

    if asset_type == "option" and legs:
        try:
            insert_spread_ledger_rows(conn, ticket_id, payload, filled_order, alpaca_order_id)
        except Exception as exc:
            reason = f"Ledger insert failed: {exc}"
            insert_decision(conn, ticket_id, "FAILED", reason)
            log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    filled_qty_str = filled_order.get("filled_qty")
    filled_qty = float(filled_qty_str) if filled_qty_str else float(payload.get("qty", 1))

    slippage = (abs(fill_price - expected_price) / expected_price
                if expected_price and expected_price > 0 else 0.0)

    if asset_type == "option":
        # Single-leg options are always bought (long premium) — engine.py's
        # place_options_order never sells outside a multi-leg order. The
        # ledger direction is the INSTRUMENT direction (drives close side
        # and PnL sign), not the market thesis: a long put is a bearish
        # position we nonetheless own, and sell to close. delta_exposure
        # carries the thesis sign via option_type instead.
        ledger_direction = "long"
        base_delta = 0.5 if payload.get("option_type", "call") == "call" else -0.5
        delta_exposure = payload.get("delta_exposure", base_delta)
    else:
        ledger_direction = direction
        delta_exposure = 1.0 if direction == "long" else -1.0

    ledger_data = {
        "bot_source": payload["bot_source"],
        "strategy_id": payload.get("strategy_id"),
        "asset": payload.get("option_symbol", symbol) if asset_type == "option" else symbol,
        "asset_type": asset_type,
        "direction": ledger_direction,
        "delta_exposure": delta_exposure,
        "notional_risk": sized_notional,
        "qty": filled_qty,
        "entry_price": fill_price,
        "entry_time": datetime.now(timezone.utc),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "alpaca_order_id": alpaca_order_id,
        "stop_pct": payload.get("stop_pct"),
        "target_pct": payload.get("target_pct"),
    }

    try:
        position_id = insert_position(conn, ledger_data)
    except Exception as exc:
        reason = f"Ledger insert failed: {exc}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    reasoning = (f"Filled at {fill_price:.4f}, slippage={slippage:.4f}, "
                 f"position_id={position_id}, alpaca_order_id={alpaca_order_id}")
    insert_decision(conn, ticket_id, "EXECUTED", reasoning)
    log_system_event(conn, "INFO", "execution_engine",
                     f"Executed {symbol} ({asset_type}) — {direction}",
                     {"ticket_id": ticket_id, "position_id": position_id,
                      "fill_price": fill_price, "slippage": slippage,
                      "strategy_id": payload.get("strategy_id")})
    logger.info("Ticket %s EXECUTED: position_id=%s fill=%.4f slippage=%.4f",
                ticket_id, position_id, fill_price, slippage)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Execution Engine starting")

    conn = get_db()

    if not try_acquire_singleton_lock(conn, "execution_engine"):
        logger.warning("execution_engine: another instance already holds the lock — skipping this cycle")
        conn.close()
        return

    try:
        upsert_heartbeat(conn)

        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.warning("no_trade_mode is SET: %s — engine exiting without trading", halt_reason)
            log_system_event(conn, "WARNING", "execution_engine",
                             f"no_trade_mode halted engine: {halt_reason}")
            return

        try:
            directives = load_master_directives()
        except Exception as exc:
            logger.error("Cannot load master-directives.json: %s", exc)
            sys.exit(1)

        try:
            run_equity_position_monitor(conn)
        except Exception as exc:
            logger.error("Position monitor error: %s", exc)
            log_system_event(conn, "ERROR", "execution_engine",
                             f"Position monitor failed: {exc}")

        close_tickets = fetch_force_close_tickets(conn)
        if close_tickets:
            logger.info("Found %d force-close tickets", len(close_tickets))
            for ticket in close_tickets:
                try:
                    if ticket.get("type") == "FORCE_CLOSE":
                        process_force_close(conn, ticket)
                    else:
                        process_close_ticket(conn, ticket)
                except Exception as exc:
                    logger.error("Unhandled error in close ticket %s: %s",
                                 ticket.get("ticket_id"), exc)
                    # A failed statement leaves the connection's transaction
                    # aborted until rolled back — without this, one bad
                    # ticket silently fails every ticket after it in the
                    # same run (every subsequent statement on this
                    # connection errors with "current transaction is
                    # aborted"), not just the one that actually failed.
                    try:
                        conn.rollback()
                    except Exception:
                        pass

        pending = fetch_pending_tickets(conn)
        logger.info("Found %d pending TRADE_REQUEST tickets", len(pending))

        if not pending:
            logger.info("No pending tickets — exiting")
            return

        for ticket in pending:
            try:
                process_ticket(conn, ticket, directives)
            except Exception as exc:
                ticket_id = str(ticket.get("ticket_id", "unknown"))
                logger.error("Unhandled error on ticket %s: %s", ticket_id, exc)
                # See matching comment in the close-ticket loop above — a
                # failed statement leaves the connection transaction aborted
                # until rolled back, which otherwise cascades into every
                # remaining ticket in this run failing too.
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    log_system_event(conn, "ERROR", "execution_engine",
                                     f"Unhandled error on ticket {ticket_id}: {exc}",
                                     {"ticket_id": ticket_id})
                except Exception:
                    pass

    finally:
        conn.close()

    logger.info("Execution Engine run complete")


if __name__ == "__main__":
    main()
