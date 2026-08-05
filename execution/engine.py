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

from ledger import (
    get_open_positions,
    insert_position,
    log_reconciliation_event,
    log_system_event,
    mark_pending_reconciliation,
    reduce_position_qty,
)

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

VALID_TIME_IN_FORCE = {"day", "gtc", "opg", "cls", "ioc", "fok"}

POLL_INTERVAL = 3
# 20x3s = 60s. Was 10 (30s): proven insufficient on 2026-08-03/08-04 — three
# consecutive EU_EXECUTOR equity market orders (VGK/EWU/FEZ), submitted right
# at the 13:30 UTC open both days, sat unfilled through the entire 30s window
# and were cancelled by finalize_order() with filled_qty=0.0 on both
# occasions. Consistent with opening-auction/cross settlement latency at
# major-exchange open rather than a genuinely invalid order — a plain market
# order on a liquid ETF (VGK/EWU/FEZ) has no other obvious reason to sit
# completely unfilled for 30 straight seconds. This is a single timeout
# constant, not a redesign: same poll/cancel/readback logic, more patience
# before giving up. Tradeoff: a ticket that's going to fail anyway now
# blocks the processing loop for up to ~63s instead of ~33s before moving to
# the next one — at current low daily ticket volume this stays well inside
# the 5-minute cron interval, but if volume grows enough that this budget
# gets tight, that's a signal for cron-level locking (already a known,
# separately-tracked gap), not for multiplying this constant further.
POLL_MAX = 20
ET_TZ = ZoneInfo("America/New_York")


class OrderIneligible(Exception):
    """
    Raised by a pre-flight check BEFORE any Alpaca API call — an order this
    system would otherwise submit is known, in advance, to be invalid (not
    shortable, malformed time_in_force, etc). Distinct from an Alpaca
    rejection: this never reaches the broker at all, so the resulting
    ticket decision is REJECTED (a business-rule veto), not FAILED (an
    actual broker communication failure).
    """
    pass

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


def load_master_directives() -> dict:
    with open(SHARED_DIR / "master-directives.json") as f:
        return json.load(f)


def alpaca_get(path: str) -> dict:
    url = f"{ALPACA_BASE_URL}/v2{path}"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    if not resp.ok:
        logger.error("Alpaca GET %s → %d: %s", path, resp.status_code, resp.text[:500])
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


# ---------------------------------------------------------------------------
# Order eligibility — checks that must pass BEFORE any order reaches Alpaca
# ---------------------------------------------------------------------------

def is_market_open() -> bool:
    """
    GET /v2/clock. Every order this system places is `type: market` with no
    extended_hours flag — Alpaca will reject any of them outright while the
    market is closed, for either asset type (options market hours mirror
    equity hours). On a clock-fetch failure, fail CLOSED (treat as not open)
    — submitting orders on an unconfirmed market state risks the exact
    "orders sent when Alpaca will immediately reject them" failure mode this
    check exists to prevent; skipping a cycle costs nothing, since pending
    tickets are preserved for the next one.
    """
    try:
        clock = alpaca_get("/clock")
        return bool(clock.get("is_open", False))
    except Exception as exc:
        logger.error("Could not fetch Alpaca clock — treating market as closed: %s", exc)
        return False


def check_shortable(symbol: str) -> tuple:
    """
    GET /v2/assets/{symbol}. Returns (eligible: bool, reason: str). Alpaca
    only allows shorting assets that are both `shortable` and
    `easy_to_borrow` — a short against anything else is rejected with a 403.
    Checked BEFORE submission so that guaranteed rejection never reaches
    Alpaca at all, rather than being discovered from the error response.
    """
    try:
        asset = alpaca_get(f"/assets/{symbol}")
    except Exception as exc:
        return False, f"Could not verify shortable status for {symbol}: {exc}"

    if not asset.get("tradable", False):
        return False, f"{symbol} is not tradable"
    if not asset.get("shortable", False):
        return False, f"{symbol} is not shortable"
    if not asset.get("easy_to_borrow", False):
        return False, f"{symbol} is shortable but hard-to-borrow (easy_to_borrow=false)"
    return True, ""


def classify_broker_error(exc: Exception) -> tuple:
    """
    Classify an Alpaca order-placement failure so it isn't handled uniformly.
    Returns (category, detail) where category is one of:
      RETRYABLE       — transient (network error, 429, 5xx). The SAME order
                         may well succeed on the next attempt; callers should
                         NOT write a terminal decision for these, so the
                         ticket stays pending and is retried next cycle.
      INVALID_ORDER   — the order itself is malformed or not currently
                         permitted (wash trade, market-hours rejection,
                         unsupported asset rule). Retrying the identical
                         order will fail again.
      POSITION_MISMATCH — broker-side qty/holdings disagree with what was
                         requested (e.g. insufficient qty available to close).
      RISK_REJECTION  — broker-side capital/permission constraint (buying
                         power, PDT, account restriction).
      UNKNOWN         — could not be classified with confidence; surfaced
                         loudly rather than silently forced into a bucket.

    Grounded in Alpaca's documented behavior: 422 is their validation-error
    status (bad/ineligible order), 403 covers wash-trade and shortable/
    buying-power/permission rejections, 429/5xx are transient. Message-level
    substring matching narrows 403/422 further where Alpaca's wording is
    fairly consistent, but defaults to UNKNOWN rather than guessing when it
    isn't recognized — this table should be refined from real production
    rejections over time, not treated as exhaustive on day one.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        # No HTTP response at all — DNS/connection/timeout. Always retryable.
        return "RETRYABLE", f"Network-level failure (no response): {exc}"

    status = resp.status_code
    try:
        body = resp.text or ""
    except Exception:
        body = ""
    msg = body.lower()

    if status == 429 or status >= 500:
        return "RETRYABLE", f"HTTP {status}: {body[:300]}"

    if status == 403:
        if "wash trade" in msg:
            return "INVALID_ORDER", f"Wash trade rejection: {body[:300]}"
        if "shortable" in msg or "hard to borrow" in msg or "borrow" in msg:
            return "INVALID_ORDER", f"Shortable/hard-to-borrow rejection: {body[:300]}"
        if "buying power" in msg or "insufficient" in msg:
            return "RISK_REJECTION", f"Buying power rejection: {body[:300]}"
        if "pattern day trad" in msg or "pdt" in msg:
            return "RISK_REJECTION", f"PDT rejection: {body[:300]}"
        return "UNKNOWN", f"HTTP 403 (unrecognized reason): {body[:300]}"

    if status == 422:
        if "qty" in msg or "quantity" in msg:
            return "POSITION_MISMATCH", f"Quantity rejection: {body[:300]}"
        if "market" in msg and ("closed" in msg or "hour" in msg):
            return "INVALID_ORDER", f"Market-hours rejection: {body[:300]}"
        # 422 is Alpaca's general request-validation status — a malformed or
        # currently-ineligible order is the most likely cause even when the
        # message doesn't match a more specific pattern above.
        return "INVALID_ORDER", f"HTTP 422 (validation): {body[:300]}"

    return "UNKNOWN", f"HTTP {status}: {body[:300]}"


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


def cancel_order(order_id: str) -> bool:
    """
    Cancel a resting order. Alpaca returns 204 (empty body) on success — do
    NOT run this through alpaca_delete, which calls resp.json() and would
    raise on an empty body. A 422 means the order already reached a terminal
    state (filled/already-canceled) — that's a no-op here, not a failure.
    """
    url = f"{ALPACA_BASE_URL}/v2/orders/{order_id}"
    resp = requests.delete(url, headers=ALPACA_HEADERS, timeout=15)
    if resp.status_code in (200, 204):
        logger.info("Cancelled order %s", order_id)
        return True
    if resp.status_code == 422:
        logger.info("Cancel order %s: already terminal — no-op", order_id)
        return False
    logger.error("Alpaca DELETE /orders/%s → %d: %s", order_id, resp.status_code, resp.text[:500])
    return False


def finalize_order(order_id: str) -> dict:
    """
    Resolve an order to its true, final, broker-confirmed state — never trust
    the requested qty as a stand-in for what actually happened. If the order
    is still open in any sense (new/accepted/pending_new/partially_filled)
    after the poll window, actively cancel it so it can't keep filling
    unsupervised, then read back Alpaca's authoritative post-cancel state
    (filled_qty reflects exactly what executed before the cancel took
    effect). A partial fill is a real, valid outcome here — it is returned
    as-is for the caller to ledger correctly, not discarded.
    """
    order = poll_order_until_filled(order_id)
    status = order.get("status", "")
    if status in ("filled", "canceled", "expired", "rejected", "done_for_day"):
        return order

    logger.warning("Order %s still '%s' after poll window — cancelling and reading back actual fill",
                    order_id, status)
    cancel_order(order_id)
    time.sleep(POLL_INTERVAL)
    try:
        return alpaca_get(f"/orders/{order_id}")
    except Exception as exc:
        logger.error("Order %s: failed to read back state after cancel: %s", order_id, exc)
        return order


def get_broker_position(symbol: str) -> dict | None:
    """
    Broker-confirmed position for `symbol`, or None if Alpaca reports no open
    position (404). This is the source of truth for CLOSE quantity — the
    ledger's own qty/notional_risk must never be used to size a close order;
    it can drift from reality (rounding, slippage between sizing and fill,
    a prior partial close). Alpaca is what actually holds the position.
    """
    url = f"{ALPACA_BASE_URL}/v2/positions/{symbol}"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    if resp.status_code == 404:
        return None
    if not resp.ok:
        logger.error("Alpaca GET /positions/%s → %d: %s", symbol, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def insert_decision(conn, ticket_id: str, decision: str, reasoning: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_decisions (ticket_id, decision, reasoning, decided_by)
            VALUES (%s, %s, %s, 'EXECUTION_ENGINE')
            """,
            (ticket_id, decision, reasoning),
        )
    conn.commit()


def fetch_pending_tickets(conn) -> list:
    """Return approved TRADE_REQUEST tickets with no EXECUTION_ENGINE decision yet."""
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
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id
                    AND d.decided_by = 'EXECUTION_ENGINE'
              )
            ORDER BY t.created_at ASC
            """,
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_force_close_tickets(conn) -> list:
    """Return FORCE_CLOSE and CLOSE_REQUEST tickets with no EXECUTION_ENGINE decision yet."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type IN ('FORCE_CLOSE', 'CLOSE_REQUEST')
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id
                    AND d.decided_by = 'EXECUTION_ENGINE'
              )
            ORDER BY t.created_at ASC
            """
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
    """
    'UNATTRIBUTED' rows (recon_agent.py::cold_start_import()) are excluded
    from the existing-exposure sum. These are pre-existing broker positions
    no bot chose to take and none can manage or close via strategy logic —
    counting them meant a single legacy import (confirmed: $90,805 of AAPL
    against a ~$94k account) could permanently consume the entire
    directional budget and block every subsequent, correctly-sized bot
    trade regardless of direction. Proven live on 2026-08-03/08-04: AUS's
    EWA/BHP/RIO were rejected on both days with near-identical "long
    exposure" figures despite zero new AUS positions actually opening —
    the number wasn't growing from real trades, it was static legacy
    exposure the bots have no way to reduce. This does not weaken the
    kill switches (drawdown >8%, VIX >40) — those key off raw account
    equity, not this ledger sum.
    """
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
            WHERE status = 'open' AND direction = %s AND bot_source != 'UNATTRIBUTED'
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


def place_equity_order(payload: dict) -> dict:
    symbol = payload["symbol"]
    sized_notional = float(payload["sized_notional"])
    time_in_force = payload["time_in_force"]
    direction = payload["direction"]

    if time_in_force not in VALID_TIME_IN_FORCE:
        raise OrderIneligible(f"Unsupported time_in_force {time_in_force!r} for {symbol} — "
                              f"must be one of {sorted(VALID_TIME_IN_FORCE)}")

    price = get_current_price(symbol)
    qty = compute_qty_from_notional(sized_notional, price)
    side = "buy" if direction == "long" else "sell"

    # This is a NEW entry (place_equity_order is never used for closes — see
    # place_close_order), so side=="sell" here unambiguously means opening a
    # new short, not selling an existing long. Alpaca rejects a short on a
    # non-shortable/hard-to-borrow asset with a 403 — check before submitting
    # rather than let that guaranteed rejection reach the broker.
    if side == "sell":
        eligible, reason = check_shortable(symbol)
        if not eligible:
            raise OrderIneligible(f"Short-sale blocked for {symbol}: {reason}")

    order_body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": time_in_force,
    }
    logger.info("Placing equity order: %s %s x%d (notional=%.2f)", side, symbol, qty, sized_notional)
    return alpaca_post("/orders", order_body)


def place_options_order(payload: dict) -> dict:
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
        logger.info("Placing mleg options order: %d legs x%d (%s)",
                    len(legs), qty, ", ".join(f"{l['side']} {l['option_symbol']}" for l in legs))
        return alpaca_post("/orders", order_body)

    # No order_class here — Alpaca rejects "simple" on options orders with a
    # 422 (only "mleg" is a valid order_class for options; a plain order
    # omits the field entirely). Regressed once already in 3c0aec6 after
    # being fixed in 09ce0c9 — do not reintroduce it.
    option_symbol = payload["option_symbol"]
    order_body = {
        "symbol": option_symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": time_in_force,
    }
    logger.info("Placing options order: buy %s x%d", option_symbol, qty)
    return alpaca_post("/orders", order_body)


def place_close_order(symbol: str, qty: float, asset_type: str, side: str = "sell") -> dict:
    """
    side defaults to "sell" (closing a long position — true for equity and
    every single-leg option position). A short option leg (only reachable
    inside a multi-leg spread) must be closed with side="buy" instead —
    callers closing a specific ledger position must pass the side that
    matches that position's own direction, not assume "sell".

    qty comes from get_broker_position()'s live Alpaca qty (a float — Alpaca
    supports fractional equity shares). Options are always whole contracts,
    so that qty is rounded to an int before formatting; a fractional string
    like "3.0" is not guaranteed to be accepted by Alpaca's options order
    validation the way "3" is.
    """
    qty_str = str(int(round(qty))) if asset_type == "option" else str(qty)
    # No order_class for options here either — same 422 as place_options_order.
    order_body = {
        "symbol": symbol,
        "qty": qty_str,
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    logger.info("Placing close order: %s %s x%s", side, symbol, qty_str)
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
        # Broker is the source of truth for close quantity — never recompute
        # it from notional/entry_price. That recompute is exactly what let
        # the ledger silently diverge from Alpaca in the first place.
        broker_pos = get_broker_position(symbol)
        broker_qty = abs(float(broker_pos["qty"])) if broker_pos else 0.0
        ledger_qty = pos.get("qty")
        ledger_qty = float(ledger_qty) if ledger_qty is not None else None

        # Close side must match the position's own direction — a long
        # position is closed by selling; a short position is closed by
        # buying to cover. This previously always fell through to
        # place_close_order's "sell" default regardless of direction, which
        # on a short position ADDS to it instead of covering it (the
        # 2026-07-28 BHP incident: a real -10 short became -20 at the
        # broker while the ledger row was marked 'closed'). Same derivation
        # process_close_ticket already uses correctly for CLOSE_REQUEST
        # tickets — this path (the equity monitor's own direct close) must
        # do the same instead of ever assuming "sell".
        close_side = "buy" if pos.get("direction") == "short" else "sell"

        if broker_qty <= 0:
            reason = f"Broker reports zero/no position for {symbol} — cannot close"
            logger.warning("Position %s: %s (ledger believed qty=%s)", position_id, reason, ledger_qty)
            log_reconciliation_event(
                conn, "close_broker_zero_position", symbol, ledger_qty, 0.0, "CRITICAL",
                "Mark ledger row suspect and run recon_agent.py --nightly",
                {"position_id": position_id},
            )
            with conn.cursor() as cur:
                cur.execute("UPDATE nwt_portfolio_ledger SET status='suspect' WHERE position_id=%s",
                            (position_id,))
            conn.commit()
            return

        if ledger_qty is not None and abs(broker_qty - ledger_qty) > 0.001:
            log_reconciliation_event(
                conn, "close_qty_mismatch", symbol, ledger_qty, broker_qty, "WARNING",
                "Using broker-confirmed qty as source of truth for this close",
                {"position_id": position_id, "exit_reason": exit_reason},
            )

        order = place_close_order(symbol, broker_qty, "equity", side=close_side)
        filled = finalize_order(order["id"])
        fill_price = float(filled.get("filled_avg_price") or current_price)
        filled_qty = float(filled.get("filled_qty") or 0)

        if filled_qty <= 0:
            reason = f"Equity close order for {symbol} did not fill — status={filled.get('status')}"
            logger.error("Position %s: %s", position_id, reason)
            log_system_event(conn, "ERROR", "execution_engine", reason, {"position_id": position_id})
            return

        slippage = abs(fill_price - current_price) / current_price if current_price > 0 else 0.0
        remaining = reduce_position_qty(conn, position_id, filled_qty, fill_price, slippage, exit_reason)

        if remaining > 0:
            log_system_event(conn, "WARNING", "execution_engine",
                             f"Partial equity close: {symbol} filled {filled_qty} of {broker_qty} side={close_side}, "
                             f"{remaining} remains open",
                             {"position_id": position_id, "exit_reason": exit_reason,
                              "filled_qty": filled_qty, "requested_qty": broker_qty, "side": close_side})
        else:
            log_system_event(conn, "INFO", "execution_engine",
                             f"Closed equity {symbol} reason={exit_reason} fill={fill_price:.4f} "
                             f"qty={filled_qty} side={close_side}",
                             {"position_id": position_id, "exit_reason": exit_reason,
                              "fill_price": fill_price, "filled_qty": filled_qty, "side": close_side})
        logger.info("Closed equity %s at %.4f reason=%s side=%s filled_qty=%.4f remaining=%.4f",
                    symbol, fill_price, exit_reason, close_side, filled_qty, remaining)
    except Exception as exc:
        # No ticket involved in this path — nothing to leave "pending" for a
        # RETRYABLE error, the position just stays open and gets re-evaluated
        # by the next 5-min monitor sweep regardless of category.
        category, detail = classify_broker_error(exc)
        reason = f"Equity close failed for {symbol} [{category}]: {detail}"
        logger.error("Position %s: %s", position_id, reason)
        log_system_event(conn, "ERROR", "execution_engine", reason,
                         {"position_id": position_id, "category": category})


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
    payload = ticket.get("payload") or {}
    symbol = payload.get("option_symbol") or payload.get("symbol", "")
    position_id = payload.get("position_id")
    exit_reason = payload.get("exit_reason", "hard_close")
    asset_type = payload.get("asset_type", "option")

    pos_direction = payload.get("direction", "long")
    ledger_qty = None
    if position_id:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT direction, qty FROM nwt_portfolio_ledger WHERE position_id = %s",
                        (position_id,))
            row = cur.fetchone()
        if row:
            if row.get("direction"):
                pos_direction = row["direction"]
            ledger_qty = float(row["qty"]) if row.get("qty") is not None else None
    close_side = "buy" if pos_direction == "short" else "sell"

    # Broker is the source of truth for close quantity. The ledger's own qty
    # (or a notional/price recompute) is never used to size this order — it
    # can drift from what Alpaca actually holds.
    broker_pos = get_broker_position(symbol)
    broker_qty = abs(float(broker_pos["qty"])) if broker_pos else 0.0

    if broker_qty <= 0:
        reason = f"Broker reports zero/no position for {symbol} — nothing to close"
        logger.warning("Ticket %s: %s (ledger believed qty=%s)", ticket_id, reason, ledger_qty)
        log_reconciliation_event(
            conn, "close_broker_zero_position", symbol, ledger_qty, 0.0, "CRITICAL",
            "Mark ledger row suspect and run recon_agent.py --nightly",
            {"ticket_id": ticket_id, "position_id": position_id},
        )
        if position_id:
            with conn.cursor() as cur:
                cur.execute("UPDATE nwt_portfolio_ledger SET status='suspect' WHERE position_id=%s",
                            (position_id,))
            conn.commit()
        insert_decision(conn, ticket_id, "FAILED", reason)
        return

    if ledger_qty is not None and abs(broker_qty - ledger_qty) > 0.001:
        log_reconciliation_event(
            conn, "close_qty_mismatch", symbol, ledger_qty, broker_qty, "WARNING",
            "Using broker-confirmed qty as source of truth for this close",
            {"ticket_id": ticket_id, "position_id": position_id, "exit_reason": exit_reason},
        )

    try:
        order = place_close_order(symbol, broker_qty, asset_type, side=close_side)
        filled = finalize_order(order["id"])
        fill_price = float(filled.get("filled_avg_price") or 0)
        fill_status = filled.get("status", "")
        filled_qty = float(filled.get("filled_qty") or 0)

        if filled_qty <= 0 or fill_price <= 0:
            insert_decision(conn, ticket_id, "FAILED",
                            f"Close order not filled — status={fill_status}")
            return

        slippage = 0.0
        remaining = None
        if position_id:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id = %s",
                            (position_id,))
                pos = cur.fetchone()
            if pos:
                pos = dict(pos)
                remaining = reduce_position_qty(conn, position_id, filled_qty, fill_price,
                                                slippage, exit_reason)
                if remaining == 0 and asset_type == "option":
                    write_trade_outcome(conn, pos, fill_price, exit_reason,
                                        payload.get("strategy_id"))

        if remaining is not None and remaining > 0:
            insert_decision(conn, ticket_id, "PARTIAL",
                            f"Partial close of {symbol}: filled {filled_qty} of {broker_qty}, "
                            f"{remaining} remains open at {fill_price:.4f}")
            log_system_event(conn, "WARNING", "execution_engine",
                             f"Partial close: {symbol} filled {filled_qty} of {broker_qty}",
                             {"ticket_id": ticket_id, "position_id": position_id,
                              "filled_qty": filled_qty, "requested_qty": broker_qty})
        else:
            insert_decision(conn, ticket_id, "EXECUTED",
                            f"Closed {symbol} qty={filled_qty} (broker-confirmed) at {fill_price:.4f} "
                            f"reason={exit_reason}")
            log_system_event(conn, "INFO", "execution_engine",
                             f"Close executed: {symbol} at {fill_price:.4f}",
                             {"ticket_id": ticket_id, "exit_reason": exit_reason,
                              "position_id": position_id, "filled_qty": filled_qty})
    except Exception as exc:
        category, detail = classify_broker_error(exc)
        reason = f"Close failed [{category}]: {detail}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        if category == "RETRYABLE":
            # Leave the ticket undecided — a permanently-FAILED close ticket
            # is a real open position that stops getting attention (the
            # options monitor's dedup window skips re-emitting a close
            # request for 2h once ANY decision exists for this ticket).
            log_system_event(conn, "WARNING", "execution_engine",
                             f"Retryable close failure, ticket left pending: {reason}",
                             {"ticket_id": ticket_id, "position_id": position_id, "category": category})
            return
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason,
                         {"ticket_id": ticket_id, "category": category})


def process_force_close(conn, ticket: dict) -> None:
    """
    Liquidate a ledger position on RISK_AGENT FORCE_CLOSE instruction.
    Uses Alpaca's close-position endpoint (whole position), polls the fill,
    and closes the ledger row with exit price + exit NBBO.
    """
    ticket_id = str(ticket["ticket_id"])
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
        reason = f"FORCE_CLOSE: position {position_id} already closed"
        logger.info("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "SKIPPED", reason)
        return

    asset = position.get("asset") or asset
    asset_type = position.get("asset_type", "option")

    exit_bid, exit_ask = get_latest_quote(asset, asset_type)
    expected_price = (exit_bid + exit_ask) / 2.0 if (exit_bid and exit_ask) else None

    try:
        order = alpaca_delete(f"/positions/{asset}")
    except Exception as exc:
        category, detail = classify_broker_error(exc)
        reason = f"FORCE_CLOSE: liquidation order failed for {asset} [{category}]: {detail}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        if category == "RETRYABLE":
            # This is a risk-driven emergency exit — leaving it pending for
            # immediate retry next cycle matters more here than anywhere
            # else in the engine. A transient network blip must not
            # permanently abandon a kill-switch/drawdown liquidation.
            log_system_event(conn, "WARNING", "execution_engine",
                             f"Retryable FORCE_CLOSE failure, ticket left pending: {reason}",
                             {"ticket_id": ticket_id, "position_id": position_id, "category": category})
            return
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason,
                         {"ticket_id": ticket_id, "category": category})
        return

    alpaca_order_id = order.get("id", "")
    try:
        filled_order = finalize_order(alpaca_order_id) if alpaca_order_id else order
    except Exception as exc:
        # The DELETE above already succeeded — Alpaca accepted the
        # liquidation order, this is only a poll/read-back failure. The
        # order may still be live/filling at the broker even though we
        # lost track of it; that's a real ledger/broker divergence risk
        # regardless of category, so this always stays FAILED for a human
        # to check against recon_agent.py rather than silently retried
        # (retrying could submit a SECOND liquidation for the same position).
        category, detail = classify_broker_error(exc)
        reason = f"FORCE_CLOSE: order poll failed [{category}]: {detail}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason,
                         {"ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id, "category": category})
        return

    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None
    filled_qty = float(filled_order.get("filled_qty") or 0)
    if fill_price is None or filled_qty <= 0:
        reason = f"FORCE_CLOSE: no fill — status={filled_order.get('status')}"
        logger.warning("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {
            "ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id,
        })
        return

    slippage = abs(fill_price - expected_price) / expected_price if expected_price and expected_price > 0 else 0.0
    # Alpaca's own close-position endpoint targets the whole position, but
    # the resulting order can still only partially fill — never assume it
    # fully closed. reduce_position_qty ledgers exactly what filled and
    # leaves the row 'open' with the reduced qty if anything remains.
    remaining = reduce_position_qty(conn, position_id, filled_qty, fill_price, slippage,
                                    "hard_close", exit_bid=exit_bid, exit_ask=exit_ask)
    reasoning = (
        f"FORCE_CLOSE filled {filled_qty} at {fill_price:.4f}, slippage={slippage:.4f}, "
        f"remaining={remaining}, position_id={position_id}, alpaca_order_id={alpaca_order_id}"
    )
    decision = "PARTIAL" if remaining > 0 else "EXECUTED"
    insert_decision(conn, ticket_id, decision, reasoning)
    log_system_event(conn, "INFO" if remaining == 0 else "WARNING", "execution_engine",
                     f"Force-closed {asset} ({asset_type}) filled={filled_qty} remaining={remaining}",
                     {"ticket_id": ticket_id, "position_id": position_id,
                      "alpaca_order_id": alpaca_order_id, "fill_price": fill_price,
                      "slippage": slippage, "filled_qty": filled_qty, "remaining": remaining})
    logger.info("Ticket %s FORCE_CLOSE %s: position=%s fill=%.4f filled_qty=%.4f remaining=%.4f",
                ticket_id, decision, position_id, fill_price, filled_qty, remaining)


# ---------------------------------------------------------------------------
# Trade ticket processing
# ---------------------------------------------------------------------------

def insert_spread_ledger_rows(conn, ticket_id: str, payload: dict,
                               filled_order: dict, alpaca_order_id: str,
                               is_partial: bool = False) -> str:
    """
    One ledger row PER LEG of a filled mleg order, tied by spread_group_id.
    Recon matches Alpaca positions per contract, so legs must be individual
    rows; the monitor values/closes the structure as a unit via the group id.

    Two failure modes are handled by flagging the row for human review
    instead of writing fabricated data:
      - A leg's fill price can't be resolved from the order response OR a
        live quote → entry_price is left NULL (never $0), and that leg is
        marked pending_reconciliation so it's excluded from stop/target
        monitoring rather than silently valued at zero.
      - The mleg order itself only partially filled (is_partial=True) →
        every leg in the group is marked pending_reconciliation regardless
        of its own price, since a partially-filled multi-leg order is a
        naked-leg-risk situation that must not be treated as a normal,
        fully-hedged open spread.

    Returns the spread_group_id.
    """
    qty = int(payload.get("qty", 1))
    spread_group_id = str(uuid.uuid4())
    filled_legs = {l.get("symbol"): l for l in (filled_order.get("legs") or [])}
    position_ids = []
    any_unresolved_price = False

    for leg in payload["legs"]:
        leg_symbol = leg["option_symbol"]
        fl = filled_legs.get(leg_symbol, {})
        leg_fill = fl.get("filled_avg_price")
        leg_fill = float(leg_fill) if leg_fill else None

        leg_bid, leg_ask = get_latest_quote(leg_symbol, "option")
        unresolved = False
        if leg_fill is None:
            # Leg fill missing from the order response — fall back to quote mid
            if leg_bid and leg_ask:
                leg_fill = (leg_bid + leg_ask) / 2.0
            else:
                # No order-response price AND no live quote — do not fabricate
                # a $0 entry_price. Leave it NULL and flag the row instead.
                unresolved = True
                any_unresolved_price = True

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
            "notional_risk": (abs(leg_fill) * 100 * qty) if leg_fill is not None else None,
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
        pid = insert_position(conn, ledger_data)
        position_ids.append(pid)

        if unresolved:
            mark_pending_reconciliation(
                conn, pid,
                f"mleg leg {leg_symbol}: no fill price in order response and no live quote available",
            )
            log_reconciliation_event(
                conn, "unresolved_leg_fill_price", leg_symbol, None, None, "CRITICAL",
                "Manually verify actual fill price against Alpaca and correct entry_price",
                {"ticket_id": ticket_id, "position_id": pid, "spread_group_id": spread_group_id},
            )

    if is_partial:
        for pid in position_ids:
            mark_pending_reconciliation(
                conn, pid,
                f"mleg order only partially filled — spread_group_id={spread_group_id} "
                "may hold a naked/unhedged leg",
            )
        log_reconciliation_event(
            conn, "partial_mleg_fill", payload.get("symbol", ""), None, None, "CRITICAL",
            "Manually verify each leg against Alpaca before treating this spread as hedged",
            {"ticket_id": ticket_id, "spread_group_id": spread_group_id,
             "position_ids": position_ids, "alpaca_order_id": alpaca_order_id},
        )

    status_note = "PARTIAL — pending_reconciliation" if is_partial else (
        "unresolved leg price — pending_reconciliation" if any_unresolved_price else "EXECUTED"
    )
    reasoning = (f"mleg {status_note} — {len(position_ids)} legs, "
                 f"spread_group_id={spread_group_id}, alpaca_order_id={alpaca_order_id}")
    insert_decision(conn, ticket_id, "PARTIAL" if is_partial else "EXECUTED", reasoning)
    log_system_event(conn, "WARNING" if (is_partial or any_unresolved_price) else "INFO", "execution_engine",
                     f"{'Partial ' if is_partial else ''}spread {payload.get('strategy_type', '')} "
                     f"on {payload.get('symbol', '')}",
                     {"ticket_id": ticket_id, "spread_group_id": spread_group_id,
                      "position_ids": position_ids, "alpaca_order_id": alpaca_order_id,
                      "strategy_id": payload.get("strategy_id"), "is_partial": is_partial})
    logger.info("Ticket %s %s (spread): group=%s legs=%d",
                ticket_id, "PARTIAL" if is_partial else "EXECUTED", spread_group_id, len(position_ids))
    return spread_group_id


def process_ticket(conn, ticket: dict, directives: dict) -> None:
    ticket_id = str(ticket["ticket_id"])
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

    try:
        if asset_type == "equity":
            entry_bid, entry_ask = get_latest_quote(symbol, "equity")
            order = place_equity_order(payload)
            expected_price = get_current_price(symbol)
        elif asset_type == "option":
            if not legs:
                option_symbol = payload.get("option_symbol", symbol)
                entry_bid, entry_ask = get_latest_quote(option_symbol, "option")
                if entry_bid and entry_ask:
                    expected_price = (entry_bid + entry_ask) / 2.0
            order = place_options_order(payload)
        else:
            reason = f"Unknown asset_type: {asset_type}"
            insert_decision(conn, ticket_id, "FAILED", reason)
            return
    except OrderIneligible as exc:
        # A pre-flight check blocked this before it ever reached Alpaca —
        # a business-rule veto (like the ones above), not a broker failure.
        reason = f"Order ineligible: {exc}"
        logger.warning("Ticket %s rejected: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {"ticket_id": ticket_id})
        return
    except Exception as exc:
        category, detail = classify_broker_error(exc)
        reason = f"Order placement failed [{category}]: {detail}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        if category == "RETRYABLE":
            # No terminal decision — leave the ticket unclaimed so the next
            # cron cycle retries it, instead of permanently failing an order
            # that may well succeed moments later.
            log_system_event(conn, "WARNING", "execution_engine",
                             f"Retryable order failure, ticket left pending: {reason}",
                             {"ticket_id": ticket_id, "category": category})
            return
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason,
                         {"ticket_id": ticket_id, "category": category})
        return

    alpaca_order_id = order["id"]

    try:
        filled_order = finalize_order(alpaca_order_id)
    except Exception as exc:
        reason = f"Order poll failed: {exc}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    fill_status = filled_order.get("status", "")
    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None
    filled_qty_str = filled_order.get("filled_qty")
    filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
    is_partial = fill_status == "partially_filled" or (0 < filled_qty and fill_status != "filled")

    # Nothing filled at all (rejected/expired/canceled with zero fill, or
    # mleg with a legitimately absent top-level price and no legs) — a real
    # failure, not a partial. mleg orders report per-leg fills; the top-level
    # price is the net debit/credit and may legitimately be absent for spreads.
    if filled_qty <= 0 or (fill_price is None and not legs):
        reason = f"Order did not fill — final status={fill_status}, filled_qty={filled_qty}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason,
                         {"ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id})
        return

    if asset_type == "option" and legs:
        try:
            insert_spread_ledger_rows(conn, ticket_id, payload, filled_order, alpaca_order_id,
                                      is_partial=is_partial)
        except Exception as exc:
            reason = f"Ledger insert failed: {exc}"
            insert_decision(conn, ticket_id, "FAILED", reason)
            log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    if is_partial:
        log_system_event(conn, "WARNING", "execution_engine",
                         f"Partial fill on entry: {symbol} filled_qty={filled_qty} status={fill_status}",
                         {"ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id,
                          "filled_qty": filled_qty})

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

    # Actual filled notional, not the pre-fill sized/requested notional —
    # a partial fill or price slippage between sizing and fill means those
    # two numbers legitimately differ, and exposure math (directional cap,
    # recon) must be computed off what actually happened.
    actual_notional = filled_qty * fill_price * (100 if asset_type == "option" else 1)

    ledger_data = {
        "bot_source": payload["bot_source"],
        "strategy_id": payload.get("strategy_id"),
        "asset": payload.get("option_symbol", symbol) if asset_type == "option" else symbol,
        "asset_type": asset_type,
        "direction": ledger_direction,
        "delta_exposure": delta_exposure,
        "notional_risk": actual_notional,
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

    reasoning = (f"{'Partially filled' if is_partial else 'Filled'} qty={filled_qty} "
                 f"at {fill_price:.4f}, slippage={slippage:.4f}, "
                 f"position_id={position_id}, alpaca_order_id={alpaca_order_id}")
    insert_decision(conn, ticket_id, "PARTIAL" if is_partial else "EXECUTED", reasoning)
    log_system_event(conn, "WARNING" if is_partial else "INFO", "execution_engine",
                     f"{'Partially executed' if is_partial else 'Executed'} {symbol} ({asset_type}) — {direction}",
                     {"ticket_id": ticket_id, "position_id": position_id,
                      "fill_price": fill_price, "slippage": slippage, "filled_qty": filled_qty,
                      "is_partial": is_partial, "strategy_id": payload.get("strategy_id")})
    logger.info("Ticket %s %s: position_id=%s fill=%.4f qty=%.4f slippage=%.4f",
                ticket_id, "PARTIAL" if is_partial else "EXECUTED", position_id, fill_price,
                filled_qty, slippage)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Execution Engine starting")

    conn = get_db()
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

        # Every order this engine places is type:market with no extended_hours
        # flag — Alpaca rejects all of them outright while the market is
        # closed, for either asset type. Block ALL order-related work for
        # this cycle up front rather than let each ticket discover that
        # individually via a guaranteed-failing API call. Nothing gets a
        # terminal decision here — pending tickets and open positions are
        # simply left for the next cycle once the market is open.
        if not is_market_open():
            logger.info("Market is closed — skipping position monitor and ticket processing this cycle")
            log_system_event(conn, "INFO", "execution_engine",
                             "Market closed — order-related processing skipped this cycle")
            return

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
