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
    claim_ticket,
    close_position,
    get_client_order_id,
    get_force_close_state,
    get_open_positions,
    has_pending_close_ticket,
    insert_position,
    insert_ticket,
    log_system_event,
    record_client_order_id,
    record_execution_attempt,
    record_force_close_outcome,
    record_force_close_unknown,
    release_advisory_lock,
    release_ticket_claim,
    renew_ticket_claim,
    schedule_close_attempt,
    transition_position_state,
    try_advisory_lock,
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


def _option_dte(option_symbol: str) -> int | None:
    """
    Days to expiry, parsed from the OCC symbol (ROOT + YYMMDD + C/P + strike*1000).
    Returns None if the symbol can't be parsed (e.g. it's an equity symbol).
    Duplicated from nwt_agents/shared_context.py's option_dte — execution/ is
    deployed separately, same pattern as log_system_event elsewhere in this file.
    """
    key = (option_symbol or "").upper().strip()
    if len(key) < 15:
        return None
    try:
        expiry = datetime.strptime(key[-15:-9], "%y%m%d").date()
    except ValueError:
        return None
    return (expiry - datetime.now(ET_TZ).date()).days


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

# Identifies this process as a claim owner (nwt_ticket_claims.claimed_by).
# crontab.txt has no overlap guard, so two engine.py invocations can
# legitimately be alive at once; each gets its own id.
WORKER_ID = f"engine:{os.getpid()}:{uuid.uuid4().hex[:8]}"

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


def market_is_open() -> bool:
    """
    Alpaca's own market clock. Fail-closed (False) if unreachable: placing a
    market order into a closed market is exactly the failure mode that
    produced the 2026-07-16 untracked-position incident — the order can't
    fill inside the poll window, and a GTC order then fills at the open long
    after the engine stopped watching it. Deferring a ticket costs one
    5-minute cron cycle; an orphaned fill costs a no_trade_mode halt.
    """
    try:
        clock = alpaca_get("/clock")
        return bool(clock.get("is_open", False))
    except Exception as exc:
        logger.warning("Market clock fetch failed (%s) — treating market as CLOSED", exc)
        return False


def get_open_orders(symbol: str) -> list:
    """Open (working) Alpaca orders for one symbol."""
    url = f"{ALPACA_BASE_URL}/v2/orders"
    resp = requests.get(url, headers=ALPACA_HEADERS,
                        params={"status": "open", "symbols": symbol, "limit": 50},
                        timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_alpaca_position(symbol: str) -> dict | None:
    """
    Current Alpaca position for one symbol, or None if flat.

    The broker's own net position is the source of truth for whether a
    trade opens new directional exposure or merely reduces/closes existing
    exposure that some OTHER ledger row (a different bot, or an
    UNATTRIBUTED import) already represents — Alpaca nets all activity in
    a symbol into a single position regardless of which strategy placed
    which order. A 404 means genuinely flat, not an error.
    """
    try:
        pos = alpaca_get(f"/positions/{symbol}")
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    qty = float(pos.get("qty", 0))
    if qty == 0:
        return None
    return {"qty": abs(qty), "side": "long" if qty > 0 else "short"}


def classify_equity_exposure(broker_pos_before: dict | None, side: str, filled_qty: float) -> dict:
    """
    Split a just-filled equity order's quantity into the portion that
    reduces pre-existing broker exposure vs. the portion that represents a
    genuinely NEW directional position — using the broker's own pre-trade
    position as the source of truth, never the ticket's claimed
    `direction`.

    This is the fix for the 2026-07-22 AAPL incident: a "short" entry
    ticket for 14 shares was recorded as a brand-new short position, when
    in reality the account already held a 300-share long (from an
    unrelated, UNATTRIBUTED import) and the sell order just trimmed it —
    Alpaca has no per-strategy sub-positions, so a sell against an existing
    long is a reduction, never a new short, unless the sell quantity
    exceeds the existing long (symmetric for buy vs. an existing short).

    Returns {"opening_qty", "reducing_qty", "reduces_existing"}:
      - reducing_qty:  portion that closed out pre-existing OPPOSITE
                       broker exposure — must NOT be recorded as a new
                       position.
      - opening_qty:   portion (if any) beyond that existing exposure —
                       the only part that may become a new ledger row.
      - reduces_existing: True if any reduction happened at all.
    """
    existing_qty = broker_pos_before["qty"] if broker_pos_before else 0.0
    existing_side = broker_pos_before["side"] if broker_pos_before else None

    opposing = (side == "sell" and existing_side == "long") or (side == "buy" and existing_side == "short")
    if not opposing or existing_qty <= 0:
        return {"opening_qty": filled_qty, "reducing_qty": 0.0, "reduces_existing": False}

    reducing_qty = min(filled_qty, existing_qty)
    opening_qty = max(filled_qty - existing_qty, 0.0)
    return {"opening_qty": opening_qty, "reducing_qty": reducing_qty, "reduces_existing": reducing_qty > 0}


def reduce_opposing_equity_rows(conn, symbol: str, opposing_direction: str, reduce_qty: float,
                                exit_price: float, exit_reason: str, note: str) -> list:
    """
    Reduce (or fully close) existing open equity ledger rows of
    `opposing_direction` for `symbol`, oldest first, until `reduce_qty` is
    exhausted. Used when a fill has been classified (classify_equity_exposure)
    as reducing pre-existing broker exposure rather than opening a new
    position — this may span rows from a DIFFERENT bot_source (including
    UNATTRIBUTED), since broker exposure doesn't respect bot attribution.

    Returns the list of position_ids touched, for audit logging by the caller.
    """
    remaining = reduce_qty
    touched = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM nwt_portfolio_ledger
            WHERE asset = %s AND asset_type = 'equity' AND status = 'open' AND direction = %s
            ORDER BY entry_time ASC
            """,
            (symbol, opposing_direction),
        )
        rows = [dict(r) for r in cur.fetchall()]

    for row in rows:
        if remaining <= 1e-9:
            break
        position_id = str(row["position_id"])
        row_qty = float(row.get("qty") or 0)
        if row_qty <= 0:
            continue
        take = min(remaining, row_qty)
        fully_closed = apply_close_fill(conn, row, take, exit_price, 0.0, exit_reason)
        touched.append(position_id)
        log_system_event(
            conn, "WARNING", "execution_engine",
            f"Cross-attribution reduction: {symbol} position {position_id} "
            f"({row.get('bot_source')}) reduced by {take:g} — {note}",
            {"position_id": position_id, "reduced_by": take, "fully_closed": fully_closed,
             "bot_source": row.get("bot_source")},
        )
        remaining -= take

    if remaining > 1e-9:
        logger.warning(
            "reduce_opposing_equity_rows: %s wanted to reduce %g %s exposure but only found "
            "%g across ledger rows — %g unaccounted for (broker exposure with no matching "
            "ledger row at all; needs reconciliation)",
            symbol, reduce_qty, opposing_direction, reduce_qty - remaining, remaining,
        )
        log_system_event(
            conn, "CRITICAL", "execution_engine",
            f"{symbol}: reduced broker exposure by {reduce_qty - remaining:g} but "
            f"{remaining:g} had no matching open ledger row — untracked broker exposure, needs reconciliation",
            {"symbol": symbol, "unaccounted_qty": remaining, "direction": opposing_direction},
        )
    return touched


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


def get_unattributed_notional(conn) -> float:
    """
    Total notional_risk currently open under bot_source='UNATTRIBUTED'.

    UNATTRIBUTED positions are real capital sitting at the broker (cold-start
    imports, --adopt-untracked, or a mislabeled entry never reconciled) — the
    2026-07-22 AAPL incident had ~$95k of a ~$97k account sitting unattributed,
    invisible to every capital/sizing calculation that only reads bot-specific
    exposure. It must count against available capital the same as any
    attributed position would.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(notional_risk), 0) FROM nwt_portfolio_ledger "
            "WHERE status = 'open' AND bot_source = 'UNATTRIBUTED'"
        )
        (total,) = cur.fetchone()
    return float(total or 0)


def _check_bot_permissions(conn, directives: dict, bot_source: str, sized_notional: float) -> tuple:
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

    # Available capital excludes notional already sitting in unattributed
    # exposure — that capital is not actually free to deploy even though raw
    # account equity doesn't distinguish it from real slack.
    unattributed_notional = get_unattributed_notional(conn)
    available_equity = max(equity - unattributed_notional, 0.0)
    if unattributed_notional > 0:
        logger.warning(
            "Synchronous veto check for %s: %.0f of %.0f equity is UNATTRIBUTED — "
            "available capital reduced to %.0f for sizing purposes",
            perm_key, unattributed_notional, equity, available_equity,
        )

    max_notional = available_equity * capital_weight * size_cap
    if sized_notional > max_notional * 1.05:  # small tolerance for rounding
        return True, (
            f"Synchronous veto: sized_notional {sized_notional:.0f} exceeds "
            f"bot_permissions.{perm_key} cap {max_notional:.0f} "
            f"(capital_weight={capital_weight}, size_cap={size_cap}, "
            f"available_equity={available_equity:.0f} after {unattributed_notional:.0f} "
            f"unattributed)"
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

    vetoed, reason = _check_bot_permissions(conn, directives, bot_source, float(payload.get("sized_notional", 0)))
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


# ---------------------------------------------------------------------------
# Broker-side idempotency
#
# No claim/lease sequencing, and no amount of in-flight order tracking
# after the fact, can fully close the window between "Alpaca accepted an
# order" and "we durably recorded that anywhere" — a process can be killed
# in that window however small application logic makes it. client_order_id
# is the actual backstop: deterministic per ticket_id, so a resubmission
# for the same ticket always carries the identical id and Alpaca itself
# rejects a second order with an id it has already seen.
# find_order_by_client_order_id lets a recovering worker check for and
# reuse that original order instead of ever attempting a resubmission.
# ---------------------------------------------------------------------------

def client_order_id_for(ticket_id: str, prefix: str = "nwt") -> str:
    """Deterministic, Alpaca-safe (<=48 char) client_order_id. Same
    ticket_id always produces the same id — that determinism is the whole
    mechanism; do not add any randomness or timestamp here."""
    return f"{prefix}-{ticket_id}"[:48]


def find_order_by_client_order_id(client_order_id: str) -> dict | None:
    """
    Check whether an order with this client_order_id already exists at the
    broker. limit=100 recent orders (status=all) is checked client-side —
    Alpaca's list endpoint is not guaranteed to support server-side
    client_order_id filtering across all API versions, and 100 is generous
    against the volume any single 5-minute engine.py cycle could plausibly
    place. Returns None (not found, or the lookup itself failed) rather than
    raising — a lookup failure must not block a legitimate first-time
    submission; the broker's own duplicate-order rejection remains the
    backstop if this check is ever wrong.
    """
    try:
        orders = alpaca_get("/orders?status=all&limit=100&nested=true")
    except Exception as exc:
        logger.warning("find_order_by_client_order_id(%s): lookup failed, proceeding as not-found: %s",
                       client_order_id, exc)
        return None
    for o in (orders if isinstance(orders, list) else []):
        if o.get("client_order_id") == client_order_id:
            return o
    return None


class ClaimLostError(Exception):
    """
    Raised when renew_ticket_claim() reports this worker no longer owns the
    ticket it believed it exclusively held. By the time this fires, an
    order may have ALREADY been submitted to the broker moments earlier —
    writing a FAILED decision here would be actively wrong, telling the
    rest of the system "nothing happened" when a real order might be
    sitting at Alpaca. Every catch site must re-raise this untouched
    (never let a local "poll failed" handler swallow it) so it reaches
    main()'s per-ticket wrapper, which logs CRITICAL and deliberately
    writes no decision (except FORCE_CLOSE, which writes an explicit
    UNKNOWN decision — see record_force_close_unknown's docstring for why
    that's a deliberate, narrower exception).
    """


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
                  'US_EXECUTOR', 'EU_EXECUTOR', 'AUS_EXECUTOR', 'CHINA_EXECUTOR',
                  'NWT_EXECUTION_AGENT'
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
# In-flight order tracking
#
# An order that has been submitted to Alpaca but has not reached a terminal
# state is a liability the system still owns. The engine used to poll for
# ~30s and then mark the ticket FAILED — while the order stayed live. Any
# later fill created a position the ledger never heard about
# (in_alpaca_not_ledger → no_trade_mode halt), and on the close path a
# still-working close order caused every retry to 422. Every submitted
# order is now recorded in nwt_inflight_orders until it terminates, and
# resolve_inflight_orders() runs at the top of every engine cycle —
# including under no_trade_mode, because resolving what we already own is
# risk accounting, not new trading.
# ---------------------------------------------------------------------------

ORDER_TERMINAL_DEAD = ("canceled", "expired", "rejected", "replaced", "stopped")
INFLIGHT_ENTRY_CANCEL_AFTER_HOURS = 24

# Close-order staleness — see db/migrate_2026_07_inflight_staleness.sql.
# A close order that's accepted but never fills (the AAPL incident: a "day"
# order submitted 2s after market close, queued indefinitely) used to sit
# in nwt_inflight_orders as status='pending' forever, silently blocking
# every future close attempt on that position via has_pending_inflight_close
# with zero visibility. Two thresholds, not a new state machine: at STALE,
# attempt one cancel and raise a visible WARNING ticket (the normal
# terminal-order path resolves the row once the cancel takes effect); at
# ESCALATE (cancel never took effect), give up waiting and retire the row,
# handing the position back to schedule_close_attempt's own bounded
# retry/FAILED_REQUIRES_HUMAN ceiling — nwt_force_close_state remains the
# single source of truth for "does this position's close need a human",
# this table never grows a second, parallel escalation state.
INFLIGHT_CLOSE_STALE_MINUTES = 30
INFLIGHT_CLOSE_ESCALATE_MINUTES = 120


def record_inflight_order(conn, ticket_id: str, alpaca_order_id: str, kind: str,
                          payload: dict, position_id: str = None,
                          exit_reason: str = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_inflight_orders
                (ticket_id, alpaca_order_id, kind, payload, position_id, exit_reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (alpaca_order_id) DO NOTHING
            """,
            (ticket_id, alpaca_order_id, kind, json.dumps(payload, default=str),
             position_id, exit_reason),
        )
    conn.commit()


def retire_inflight_order(conn, inflight_id: str, status: str, resolution: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_inflight_orders
            SET status=%s, resolution=%s, resolved_at=NOW()
            WHERE id=%s
            """,
            (status, resolution, inflight_id),
        )
    conn.commit()


def has_pending_inflight_close(conn, position_id: str = None, symbol: str = None) -> bool:
    """True if a close order for this position/symbol is already working."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_inflight_orders
            WHERE status='pending' AND kind='close'
              AND (position_id = %s OR payload->>'symbol' = %s)
            """,
            (position_id, symbol),
        )
        return cur.fetchone()[0] > 0


def fetch_pending_inflight(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_inflight_orders WHERE status='pending' ORDER BY created_at ASC"
        )
        return [dict(r) for r in cur.fetchall()]


def resolve_inflight_orders(conn) -> None:
    """
    Poll every pending in-flight order once. Fills produce the same ledger/
    outcome writes an immediate fill would have; dead orders retire the row
    with a FAILED ticket decision; entry orders still working after
    INFLIGHT_ENTRY_CANCEL_AFTER_HOURS get a cancel request (the cancel drives
    them terminal on a later cycle). Close orders are never auto-canceled —
    a working close is still reducing risk.

    Guarded by a process-level advisory lock: fetch_pending_inflight()'s row
    fetch has no per-row claim, so two concurrent engine.py invocations
    (crontab.txt has no flock) could otherwise both resolve the same
    in-flight row. Skips this whole cycle's resolution (not fatal — the next
    cron tick 5 minutes later retries) if another instance already holds it.
    """
    if not try_advisory_lock(conn, "resolve_inflight_orders"):
        logger.warning("resolve_inflight_orders: another instance already holds this lock — skipping this cycle")
        return
    try:
        _resolve_inflight_orders_locked(conn)
    finally:
        release_advisory_lock(conn, "resolve_inflight_orders")


def _resolve_inflight_orders_locked(conn) -> None:
    for row in fetch_pending_inflight(conn):
        inflight_id = str(row["id"])
        order_id = row["alpaca_order_id"]
        ticket_id = str(row["ticket_id"]) if row.get("ticket_id") else None
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)

        try:
            order = alpaca_get(f"/orders/{order_id}")
        except Exception as exc:
            logger.warning("In-flight %s: order %s poll failed: %s", inflight_id, order_id, exc)
            continue

        status = order.get("status", "")
        filled_qty = float(order.get("filled_qty") or 0)
        fill_price = order.get("filled_avg_price")
        fill_price = float(fill_price) if fill_price else None
        is_spread = bool(payload.get("legs"))

        if status == "filled" or (status == "done_for_day" and filled_qty > 0):
            try:
                if row["kind"] == "entry":
                    record_entry_fill(conn, ticket_id, payload, order, order_id,
                                      note="resolved from in-flight")
                else:
                    _record_close_fill(conn, row, order, fill_price)
                retire_inflight_order(conn, inflight_id, "resolved", "filled")
                logger.info("In-flight %s (%s) RESOLVED filled: order=%s",
                            inflight_id, row["kind"], order_id)
            except Exception as exc:
                logger.error("In-flight %s: fill recording failed: %s", inflight_id, exc)
                log_system_event(conn, "ERROR", "execution_engine",
                                 f"In-flight fill recording failed for order {order_id}: {exc}",
                                 {"inflight_id": inflight_id, "ticket_id": ticket_id})
            continue

        if (status in ORDER_TERMINAL_DEAD or status == "done_for_day") and filled_qty > 0 and (fill_price or is_spread):
            # Died with a partial fill — those shares/contracts are real.
            try:
                if row["kind"] == "entry":
                    record_entry_fill(conn, ticket_id, payload, order, order_id,
                                      note=f"partial fill, order {status} (in-flight)")
                else:
                    _record_close_fill(conn, row, order, fill_price)
                retire_inflight_order(conn, inflight_id, "resolved", f"partial_{status}")
                logger.warning("In-flight %s (%s) resolved with PARTIAL fill: order=%s status=%s",
                               inflight_id, row["kind"], order_id, status)
            except Exception as exc:
                logger.error("In-flight %s: partial-fill recording failed: %s", inflight_id, exc)
                log_system_event(conn, "ERROR", "execution_engine",
                                 f"In-flight partial-fill recording failed for order {order_id}: {exc}",
                                 {"inflight_id": inflight_id, "ticket_id": ticket_id})
            continue

        if status in ORDER_TERMINAL_DEAD or (status == "done_for_day" and filled_qty == 0):
            reason = f"In-flight order {order_id} terminated without fill: status={status}"
            retire_inflight_order(conn, inflight_id, "dead", status)
            if ticket_id:
                insert_decision(conn, ticket_id, "FAILED", reason)
            log_system_event(conn, "WARNING", "execution_engine", reason,
                             {"inflight_id": inflight_id, "ticket_id": ticket_id})
            logger.warning(reason)
            continue

        # Still working. Entries that have been live too long get canceled —
        # a signal sized against yesterday's prices must not fill tomorrow.
        with conn.cursor() as cur:
            cur.execute("UPDATE nwt_inflight_orders SET last_checked_at = NOW() WHERE id = %s", (inflight_id,))
        conn.commit()

        age_hours = (datetime.now(timezone.utc) - row["created_at"]).total_seconds() / 3600
        if row["kind"] == "entry" and age_hours > INFLIGHT_ENTRY_CANCEL_AFTER_HOURS:
            try:
                url = f"{ALPACA_BASE_URL}/v2/orders/{order_id}"
                resp = requests.delete(url, headers=ALPACA_HEADERS, timeout=15)
                if resp.status_code in (200, 204):
                    logger.info("In-flight %s: cancel requested for stale entry order %s",
                                inflight_id, order_id)
                # keep row pending: the cancel resolves it terminally next cycle
            except Exception as exc:
                logger.warning("In-flight %s: cancel request failed: %s", inflight_id, exc)
        elif row["kind"] == "close":
            _handle_stale_close_inflight(conn, row, order, age_hours * 60)


def _handle_stale_close_inflight(conn, row: dict, order: dict, age_minutes: float) -> None:
    """
    A close order is still working (not filled, not terminal) past
    INFLIGHT_CLOSE_STALE_MINUTES. Two thresholds:

      STALE (default 30 min): mark stale_since (once — this branch never
      re-fires for the same row) and request ONE cancel. If the cancel
      takes effect, the order goes terminal (canceled) and resolves through
      the ORDER_TERMINAL_DEAD branch on a later poll exactly like any other
      dead order — no special-casing needed there. A WARNING ticket makes
      this visible immediately rather than waiting for that resolution.

      ESCALATE (default 120 min): the cancel never took effect (or the
      order is stuck in a state Alpaca itself won't move past). Stop
      waiting — retire the row as dead/requires_human and record a
      retryable failure against nwt_force_close_state, handing the
      position back to schedule_close_attempt's own bounded ceiling rather
      than inventing a second escalation path here.
    """
    inflight_id = str(row["id"])
    ticket_id = str(row["ticket_id"]) if row.get("ticket_id") else None
    position_id = row.get("position_id")
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    symbol = payload.get("symbol", "")
    order_id = order.get("id", "")

    if age_minutes >= INFLIGHT_CLOSE_ESCALATE_MINUTES:
        reason = (f"Close order {order_id} for {symbol} pending {age_minutes:.0f}m with no fill "
                 f"and no terminal status — giving up on this attempt, needs human review")
        retire_inflight_order(conn, inflight_id, "dead", "requires_human_stale_timeout")
        if position_id:
            record_force_close_outcome(conn, position_id, success=False, error=reason)
        insert_ticket(conn, "EXECUTION_ENGINE", "SYSTEM", "inflight_close_stuck", {
            "inflight_id": inflight_id, "ticket_id": ticket_id, "position_id": position_id,
            "symbol": symbol, "order_id": order_id, "age_minutes": round(age_minutes, 1),
        })
        log_system_event(conn, "CRITICAL", "execution_engine", reason,
                         {"inflight_id": inflight_id, "position_id": position_id, "symbol": symbol})
        logger.critical(reason)
        return

    if age_minutes >= INFLIGHT_CLOSE_STALE_MINUTES and row.get("stale_since") is None:
        with conn.cursor() as cur:
            cur.execute("UPDATE nwt_inflight_orders SET stale_since = NOW() WHERE id = %s", (inflight_id,))
        conn.commit()

        cancel_ok = False
        try:
            url = f"{ALPACA_BASE_URL}/v2/orders/{order_id}"
            resp = requests.delete(url, headers=ALPACA_HEADERS, timeout=15)
            cancel_ok = resp.status_code in (200, 204)
        except Exception as exc:
            logger.warning("In-flight %s: stale-close cancel request failed: %s", inflight_id, exc)

        reason = (f"Close order {order_id} for {symbol} pending {age_minutes:.0f}m with no fill — "
                 f"marked stale, cancel {'requested' if cancel_ok else 'attempt failed'}")
        insert_ticket(conn, "EXECUTION_ENGINE", "SYSTEM", "inflight_stale_warning", {
            "inflight_id": inflight_id, "ticket_id": ticket_id, "position_id": position_id,
            "symbol": symbol, "order_id": order_id, "age_minutes": round(age_minutes, 1),
            "cancel_requested": cancel_ok,
        })
        log_system_event(conn, "WARNING", "execution_engine", reason,
                         {"inflight_id": inflight_id, "position_id": position_id, "symbol": symbol})
        logger.warning(reason)


def _record_close_fill(conn, inflight_row: dict, order: dict, fill_price: float) -> None:
    """A tracked close order filled — close the ledger row + write the outcome."""
    payload = inflight_row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    position_id = str(inflight_row.get("position_id") or "")
    exit_reason = inflight_row.get("exit_reason") or "hard_close"
    ticket_id = str(inflight_row["ticket_id"]) if inflight_row.get("ticket_id") else None
    asset_type = payload.get("asset_type", "option")
    symbol = payload.get("symbol", "")

    if fill_price is None or fill_price <= 0:
        raise RuntimeError(f"close order {order.get('id')} filled without a fill price")

    if position_id:
        pos = get_ledger_position(conn, position_id)
        if pos and pos.get("status") != "closed":
            closed_qty = float(order.get("filled_qty") or pos.get("qty") or 0)
            apply_close_fill(conn, pos, closed_qty, fill_price, 0.0, exit_reason,
                             payload.get("strategy_id"))
    if ticket_id:
        insert_decision(conn, ticket_id, "EXECUTED",
                        f"Closed {symbol} at {fill_price:.4f} reason={exit_reason} "
                        f"(resolved from in-flight)")
    log_system_event(conn, "INFO", "execution_engine",
                     f"In-flight close resolved: {symbol} at {fill_price:.4f}",
                     {"position_id": position_id, "exit_reason": exit_reason})


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
                    len(legs), qty, ", ".join(f"{l['side']} {l['option_symbol']}" for l in legs), client_order_id)
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
    ledger_qty = position.get("qty")
    if ledger_qty:
        qty = max(int(float(ledger_qty)), 1)
    else:
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


def apply_close_fill(conn, pos: dict, filled_qty: float, fill_price: float,
                     slippage: float, exit_reason: str, strategy_id: str = None,
                     exit_bid: float = None, exit_ask: float = None) -> bool:
    """
    Close a ledger position against the qty the close order ACTUALLY filled —
    never against what the ticket asked for. A close that fills fewer
    contracts than the row holds shrinks the row (qty and notional_risk
    scaled down, outcome written for the sold portion only) and leaves it
    open; marking it fully closed while contracts remain at Alpaca is how
    3 unsold AAPL contracts rode into expiry with a flat ledger.
    Returns True if the row was fully closed.
    """
    position_id = str(pos["position_id"])
    row_qty = float(pos.get("qty") or 0)

    if row_qty > 0 and filled_qty > 0 and filled_qty < row_qty - 1e-9:
        fraction = filled_qty / row_qty
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE nwt_portfolio_ledger
                SET qty = qty - %s, notional_risk = notional_risk * (1 - %s)
                WHERE position_id = %s
                """,
                (filled_qty, fraction, position_id),
            )
        conn.commit()
        if pos.get("asset_type") == "option":
            part = dict(pos)
            part["qty"] = filled_qty
            part["notional_risk"] = float(pos.get("notional_risk") or 0) * fraction
            write_trade_outcome(conn, part, fill_price, exit_reason, strategy_id)
        log_system_event(conn, "WARNING", "execution_engine",
                         f"PARTIAL close: {pos.get('asset')} filled {filled_qty:g} of "
                         f"{row_qty:g} — row stays open with the remainder",
                         {"position_id": position_id, "filled_qty": filled_qty,
                          "row_qty": row_qty, "exit_reason": exit_reason})
        logger.warning("PARTIAL close %s: %g/%g filled — remainder stays open",
                       pos.get("asset"), filled_qty, row_qty)
        return False

    actually_closed = close_position(conn, position_id, fill_price, slippage, exit_reason,
                                     exit_bid=exit_bid, exit_ask=exit_ask)
    if not actually_closed:
        # close_position() is idempotent (WHERE status='open') — this
        # position was already closed by a different, racing attempt (e.g.
        # a second FORCE_CLOSE/CLOSE_REQUEST ticket for the same position).
        # write_trade_outcome() must NOT run again here: a second call
        # would write a duplicate outcome row for a close that already has
        # one, corrupting PnL/attribution for this trade.
        logger.warning("apply_close_fill: position %s was already closed by another attempt — "
                       "not writing a duplicate trade outcome", position_id)
        return True
    transition_position_state(conn, position_id, "CLOSED", f"filled: {exit_reason}", "execution_engine")
    if pos.get("asset_type") == "option":
        write_trade_outcome(conn, pos, fill_price, exit_reason, strategy_id)
    return True


# ---------------------------------------------------------------------------
# Position monitor (equity multi-day exits)
# ---------------------------------------------------------------------------

def run_equity_position_monitor(conn) -> None:
    """
    Evaluate every open equity position for stop/target/max-hold exit.

    This function (and everything it calls) must NEVER place a broker
    order directly — it only creates CLOSE_REQUEST tickets via
    _emit_equity_close_request(). The actual close (dedup, claim,
    client_order_id, in-flight tracking, partial-fill handling, broker
    call, ledger update) happens later in this same main() run via the
    normal fetch_force_close_tickets() -> process_close_ticket() pipeline,
    exactly like options exits — a direct-broker-call equity close path
    used to exist here with no nwt_tickets row, no claim, and no execution
    decision, the weakest-audited path in the system.
    """
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

        # State-based dedup (has_pending_close_ticket), not a time window —
        # a position with an already-outstanding, undecided CLOSE_REQUEST
        # never gets a second one, no matter how many monitor cycles pass
        # before engine.py's own close-tickets loop actually consumes it.
        if has_pending_close_ticket(conn, position_id):
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
                # A failed query here leaves the connection's transaction
                # aborted until rolled back — every subsequent query on
                # this same conn (has_pending_close_ticket/insert_ticket for
                # THIS position, and every other position later in this
                # same loop) would otherwise fail with "current transaction
                # is aborted" even though this specific failure is meant to
                # be non-fatal (falls back to the hardcoded defaults above).
                conn.rollback()

        # exit_reason values are unchanged from before this refactor — 'stop',
        # 'target', 'max_hold' — matching close_position()'s documented
        # vocabulary and whatever else in the codebase (performance/tracker.py,
        # session_scorecard.py) already expects these exact strings.
        exit_reason = None
        entry_time = pos.get("entry_time")
        if entry_time:
            age_days = (datetime.now(timezone.utc) - entry_time.replace(tzinfo=timezone.utc)).days
            if age_days >= max_hold_days:
                exit_reason = "max_hold"

        if exit_reason is None:
            if pnl_pct <= stop_pct:
                exit_reason = "stop"
            elif pnl_pct >= target_pct:
                exit_reason = "target"

        if exit_reason:
            _emit_equity_close_request(conn, pos, position_id, symbol, notional, entry_price, exit_reason)


def _emit_equity_close_request(conn, pos: dict, position_id: str, symbol: str,
                               notional: float, entry_price: float, exit_reason: str) -> None:
    """
    Create a CLOSE_REQUEST ticket for an equity exit — this is the ONLY
    thing this function is allowed to do. process_close_ticket() (dedup,
    claim, client_order_id, in-flight tracking, partial-fill handling,
    broker call, ledger update) owns everything downstream, identical to
    how options exits already work. qty prefers the ledger row's real
    filled qty over a notional/entry_price recomputation — the same fix
    that keeps process_close_ticket from under-selling a position whose
    entry qty was rounded.

    Gated by schedule_close_attempt: a position that keeps failing to close
    (e.g. Alpaca rejects the qty — a long/short netting collision in the
    same symbol, or genuine ledger/broker drift) gets bounded retries with
    backoff instead of a fresh CLOSE_REQUEST every single monitor cycle
    forever. See db/migrate_2026_07_execution_safety.sql's
    nwt_force_close_state — this is the same state machine FORCE_CLOSE
    already uses, shared per position_id regardless of ticket type.
    """
    if not schedule_close_attempt(conn, position_id, symbol):
        logger.info("Equity close for %s (position_id=%s) not scheduled — terminal, cooling "
                    "off, or already in flight", symbol, position_id)
        return

    ledger_qty = pos.get("qty")
    qty = int(float(ledger_qty)) if ledger_qty else compute_qty_from_notional(notional, entry_price)
    try:
        ticket_id = insert_ticket(
            conn,
            from_agent="EQUITY_MONITOR",
            to_agent="EXECUTION_ENGINE",
            type_="CLOSE_REQUEST",
            payload={
                "approved": True,
                "bot_source": pos.get("bot_source", "EQUITY_MONITOR"),
                "symbol": symbol,
                "position_id": position_id,
                "asset_type": "equity",
                "direction": pos.get("direction", "long"),
                "strategy_id": pos.get("bot_source", ""),
                "qty": qty,
                "sized_notional": notional,
                "time_in_force": "day",
                "exit_reason": exit_reason,
                "trigger_source": "EQUITY_MONITOR",
            },
        )
        logger.info("Equity close requested: %s position_id=%s reason=%s ticket=%s",
                    symbol, position_id, exit_reason, ticket_id)
    except Exception as exc:
        logger.error("Failed to create CLOSE_REQUEST for equity position %s: %s", position_id, exc)


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
    qty = int(payload.get("qty", 1))

    pos_direction = payload.get("direction", "long")
    ledger_pos = get_ledger_position(conn, position_id) if position_id else None

    # Never process a CLOSE_REQUEST for a position already closed — mirrors
    # process_force_close's identical guard. Without this, a second close
    # ticket racing/lagging behind a first one that already succeeded would
    # submit a real order against a position that no longer exists at the
    # broker (the exact mechanism behind the 2026-07-22 BHP phantom short:
    # a duplicate close ticket sold into a zero position, and Alpaca opened
    # a new short instead of rejecting it).
    if ledger_pos and ledger_pos.get("status") == "closed":
        reason = f"CLOSE_REQUEST: position {position_id} already closed"
        logger.info("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "SKIPPED", reason)
        return

    if ledger_pos:
        if ledger_pos.get("direction"):
            pos_direction = ledger_pos["direction"]
        # The order qty must be the ledger row's REAL fill qty, not whatever
        # the ticket carried — a notional-derived ticket qty under-sold the
        # AAPL260717 close (3 of 6 contracts) and the rest rode into expiry.
        if ledger_pos.get("qty"):
            qty = max(int(float(ledger_pos["qty"])), 1)
    close_side = "buy" if pos_direction == "short" else "sell"

    # Broker-position validation before submitting an equity close: the
    # ledger's belief about direction/qty is not proof anything real backs
    # it (see record_entry_fill's cross-attribution handling — a "short"
    # row can be a mislabeled trim of someone else's long). Confirm the
    # broker actually holds a matching position in the expected direction
    # before ever placing the order — never submit into what would be a
    # rejected/phantom-creating close, and never let it retry forever
    # against a position that structurally cannot be closed this way.
    if asset_type == "equity":
        try:
            broker_pos = get_alpaca_position(symbol)
        except Exception as exc:
            logger.warning("Ticket %s: broker position check failed for %s (%s) — "
                           "proceeding without this guard", ticket_id, symbol, exc)
            broker_pos = "unknown"
        if broker_pos != "unknown":
            broker_matches = (
                broker_pos is not None
                and broker_pos["side"] == pos_direction
                and broker_pos["qty"] >= qty - 0.5
            )
            if not broker_matches:
                reason = (
                    f"CLOSE_REQUEST: broker position for {symbol} does not match ledger's "
                    f"{pos_direction} qty={qty} (broker shows {broker_pos!r}) — refusing to "
                    f"submit a close that would either be rejected or create phantom exposure; "
                    f"needs reconciliation"
                )
                logger.error("Ticket %s: %s", ticket_id, reason)
                insert_decision(conn, ticket_id, "FAILED_REQUIRES_HUMAN", reason)
                log_system_event(conn, "CRITICAL", "execution_engine", reason,
                                 {"ticket_id": ticket_id, "position_id": position_id,
                                  "symbol": symbol, "broker_position": broker_pos})
                return

    # Never stack a second close order on top of a working one — the retry
    # 422s ("insufficient qty available") because the first order is already
    # holding the position's qty. Check both our own in-flight ledger and
    # Alpaca's open orders (covers orders placed before in-flight tracking
    # existed, or by a human).
    if has_pending_inflight_close(conn, position_id, symbol):
        insert_decision(conn, ticket_id, "SKIPPED",
                        f"Close already in flight for {symbol} — resolver owns it")
        return
    try:
        if any(o for o in get_open_orders(symbol) if o.get("side") == close_side):
            insert_decision(conn, ticket_id, "SKIPPED",
                            f"An open {close_side} order already exists at Alpaca for {symbol} "
                            f"— not stacking a second close")
            log_system_event(conn, "WARNING", "execution_engine",
                             f"Close skipped — untracked open {close_side} order exists for {symbol}",
                             {"ticket_id": ticket_id, "position_id": position_id})
            return
    except Exception as exc:
        logger.warning("Open-order check failed for %s (%s) — proceeding with close", symbol, exc)

    client_order_id = client_order_id_for(ticket_id, prefix="nwt-close")
    record_client_order_id(conn, ticket_id, client_order_id)  # submission begins now
    existing_order = find_order_by_client_order_id(client_order_id)
    if existing_order:
        logger.warning(
            "Ticket %s: close order with client_order_id=%s already exists at broker "
            "(id=%s, status=%s) — recovering instead of resubmitting",
            ticket_id, client_order_id, existing_order.get("id"), existing_order.get("status"),
        )

    try:
        order = existing_order or place_close_order(symbol, qty, asset_type, client_order_id, side=close_side)
        record_execution_attempt(conn, ticket_id, "submit_close", "accepted",
                                 client_order_id=client_order_id, broker_order_id=order.get("id"),
                                 payload={"symbol": symbol, "qty": qty, "side": close_side})
        # The order above is real at the broker now — see the identical
        # note in process_ticket for why the claim MUST be renewed and
        # checked here, before polling.
        if not renew_ticket_claim(conn, ticket_id, WORKER_ID):
            # Mirrors process_force_close's identical handling: the order
            # above is already real at the broker, so losing the claim here
            # means the outcome is genuinely unknown, not FAILED — a human
            # or reconcile_unknown_force_close must resolve it against live
            # broker state before anything retries.
            if position_id:
                record_force_close_unknown(conn, position_id, symbol, ticket_id, WORKER_ID)
            raise ClaimLostError(
                f"Lost claim ownership for ticket {ticket_id} after close order was already "
                f"submitted — another worker may already be processing it"
            )
        filled = poll_order_until_filled(order["id"])
        fill_price = float(filled.get("filled_avg_price") or 0)
        fill_status = filled.get("status", "")

        if fill_status in ORDER_TERMINAL_DEAD and fill_price <= 0:
            insert_decision(conn, ticket_id, "FAILED",
                            f"Close order died unfilled — status={fill_status}")
            return

        if fill_status != "filled" or fill_price <= 0:
            # Order still working — the position is NOT closed yet, but the
            # order is live. Hand it to the in-flight resolver instead of
            # declaring failure and re-closing into a 422 next cycle.
            record_inflight_order(
                conn, ticket_id, order["id"], "close",
                {"symbol": symbol, "asset_type": asset_type, "qty": qty,
                 "strategy_id": payload.get("strategy_id")},
                position_id=position_id, exit_reason=exit_reason,
            )
            insert_decision(conn, ticket_id, "SUBMITTED",
                            f"Close order {order['id']} still working (status={fill_status}) "
                            f"— recorded in-flight")
            return

        fully_closed = True
        if ledger_pos:
            closed_qty = float(filled.get("filled_qty") or qty)
            fully_closed = apply_close_fill(conn, ledger_pos, closed_qty, fill_price,
                                            0.0, exit_reason, payload.get("strategy_id"))

        insert_decision(conn, ticket_id, "EXECUTED",
                        f"Closed {symbol} at {fill_price:.4f} reason={exit_reason}"
                        + ("" if fully_closed else " (PARTIAL — remainder stays open)"))
        log_system_event(conn, "INFO", "execution_engine",
                         f"Close executed: {symbol} at {fill_price:.4f}",
                         {"ticket_id": ticket_id, "exit_reason": exit_reason,
                          "position_id": position_id})
        if fully_closed and position_id:
            record_force_close_outcome(conn, position_id, success=True)
    except ClaimLostError:
        raise  # propagate untouched — see ClaimLostError's docstring
    except Exception as exc:
        reason = f"Close failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        if position_id:
            terminal, class_reason = _classify_close_order_failure(symbol, asset_type, exc)
            record_force_close_outcome(conn, position_id, success=False, error=str(exc),
                                       terminal=terminal, terminal_reason=class_reason if terminal else None)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        record_execution_attempt(conn, ticket_id, "submit_close", "error",
                                 client_order_id=client_order_id,
                                 error_state=str(status_code or type(exc).__name__),
                                 payload={"symbol": symbol, "qty": qty})


def _is_broker_qty_mismatch_error(exc: Exception) -> bool:
    """
    True if this exception is Alpaca's specific "insufficient qty available"
    rejection (code 40310000) on a POST /v2/orders close attempt — most
    commonly a long/short netting collision (Alpaca nets opposite-direction
    ledger positions in the same symbol into one broker-side position, so
    closing the larger leg while the smaller opposite leg is still open can
    transiently fail) but can also indicate genuine ledger/broker qty drift.
    Either way it needs a distinct, searchable label rather than being
    lumped in with generic order-placement failures — see
    recon_agent.py's signed net-exposure check for the actual disambiguation.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    try:
        body = resp.json()
    except Exception:
        body = {}
    if isinstance(body, dict) and body.get("code") == 40310000:
        return True
    text = (getattr(resp, "text", "") or "").lower()
    return "insufficient qty available" in text


def _classify_close_order_failure(asset: str, asset_type: str, exc: Exception) -> tuple:
    """
    Decide whether a failed CLOSE_REQUEST order submission (POST
    /v2/orders) is terminal or retryable. Distinct from
    _classify_force_close_failure (below): that one is written for the
    FORCE_CLOSE DELETE endpoint, where a 404 specifically means "already
    closed" — that doesn't hold for POST /orders, where a 404 would mean
    something else entirely (bad symbol), so the two are not
    interchangeable despite similar shape.
    Returns (terminal: bool, reason: str).
    """
    if _is_broker_qty_mismatch_error(exc):
        # Retryable, not terminal — schedule_close_attempt's own backoff and
        # FORCE_CLOSE_MAX_ATTEMPTS ceiling (not this function) decide when to
        # stop retrying and escalate to FAILED_REQUIRES_HUMAN.
        return False, "BROKER_QTY_MISMATCH: insufficient qty available at broker — possible long/short netting collision or ledger drift, see recon"
    if asset_type == "option":
        dte = _option_dte(asset)
        if dte is not None and dte < 0:
            return True, f"Option expired {abs(dte)}d ago — awaiting broker auto-settlement"
    return False, str(exc)


def _classify_force_close_failure(asset: str, asset_type: str, exc: Exception) -> tuple:
    """
    Decide whether a failed DELETE attempt is terminal (never going to
    succeed, stop retrying) or retryable (transient, try again later).
    status_code == 404 -> not a failure at all: the broker has no such
    position, i.e. it's already closed. An expired option has no market
    left to submit a closing order against — terminal. Everything else
    (network blips, 403s that clear up, market-hours rejections) is
    retryable, bounded by schedule_force_close_attempt's backoff.
    Returns (already_closed: bool, terminal: bool, reason: str).
    """
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 404:
        return True, False, "Alpaca reports no such position — already closed"
    if asset_type == "option":
        dte = _option_dte(asset)
        if dte is not None and dte < 0:
            return False, True, f"Option expired {abs(dte)}d ago — awaiting broker auto-settlement"
    return False, False, str(exc)


def reconcile_unknown_force_close(conn, position_id: str, asset: str) -> str | None:
    """
    If this position's force-close state is UNKNOWN (a prior attempt's
    worker lost its claim after the liquidation DELETE may have already
    reached the broker, so record_force_close_outcome() was never called),
    resolve it now by querying live broker state before doing anything
    else:
      - no position at the broker (404)   -> the earlier attempt DID
        succeed. Close the ledger row if it isn't already, mark SUCCESS.
      - position still open at the broker -> the earlier attempt did NOT
        succeed. Mark FAILED_RETRYABLE so the normal backoff/escalation
        machinery in schedule_force_close_attempt picks it back up.
      - broker query itself fails (network blip, 5xx) -> leave UNKNOWN in
        place rather than guessing.
    Returns 'SUCCESS' or 'FAILED_RETRYABLE' if it resolved something, None
    if there was nothing to reconcile or the broker query itself failed.
    """
    state = get_force_close_state(conn, position_id)
    if not state or state.get("state") != "UNKNOWN":
        return None

    logger.warning(
        "Reconciling UNKNOWN force-close outcome for position %s (%s) — querying broker "
        "before proceeding with a new attempt", position_id, asset,
    )
    try:
        alpaca_get(f"/positions/{asset}")
        position_still_open = True
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code != 404:
            logger.warning("Reconciliation: broker query failed for %s, leaving UNKNOWN: %s", asset, exc)
            return None
        position_still_open = False

    if position_still_open:
        record_force_close_outcome(
            conn, position_id, success=False,
            error="Reconciled from UNKNOWN — broker still shows the position open",
        )
        logger.warning("Reconciled position %s: still open at broker -> FAILED_RETRYABLE", position_id)
        return "FAILED_RETRYABLE"

    close_position(conn, position_id, 0.0, 0.0, "already_closed_at_broker")
    transition_position_state(conn, position_id, "CLOSED",
                              "reconcile_unknown_force_close: broker confirms closed", "execution_engine")
    record_force_close_outcome(conn, position_id, success=True)
    logger.info("Reconciled position %s: broker confirms closed -> SUCCESS", position_id)
    return "SUCCESS"


def process_force_close(conn, ticket: dict) -> None:
    """
    Liquidate a ledger position on RISK_AGENT FORCE_CLOSE instruction.
    Uses Alpaca's close-position endpoint (whole position), polls the fill,
    and closes the ledger row with exit price + exit NBBO.

    Every outcome is recorded against nwt_force_close_state
    (record_force_close_outcome / record_force_close_unknown) — the half
    of the force-close state machine that classifies what actually
    happened; risk_agent.py's schedule_force_close_attempt() owns
    whether/when to retry based on what gets recorded here.
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

    # Reconcile a prior UNKNOWN outcome (a previous attempt's worker lost
    # its claim after the broker DELETE may have already succeeded, so
    # nothing was ever recorded) BEFORE doing anything else — otherwise
    # this attempt would blindly retry on top of an outcome nobody knows.
    resolved_asset = position.get("asset") or asset
    reconciled = reconcile_unknown_force_close(conn, position_id, resolved_asset)
    if reconciled == "SUCCESS":
        insert_decision(conn, ticket_id, "SKIPPED",
                        f"FORCE_CLOSE: reconciled a prior UNKNOWN outcome — broker confirms "
                        f"{resolved_asset} already closed")
        return
    if reconciled == "FAILED_RETRYABLE":
        insert_decision(conn, ticket_id, "SKIPPED",
                        f"FORCE_CLOSE: reconciled a prior UNKNOWN outcome — broker shows "
                        f"{resolved_asset} still open, marked for retry")
        return
    # reconciled is None: either state wasn't UNKNOWN (normal case), or the
    # broker query itself failed transiently — fall through to a normal
    # attempt rather than blocking forever on a flaky reconciliation check.

    if position.get("status") == "closed":
        reason = f"FORCE_CLOSE: position {position_id} already closed"
        logger.info("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "SKIPPED", reason)
        if position_id:
            record_force_close_outcome(conn, position_id, success=True)
        return

    asset = position.get("asset") or asset
    asset_type = position.get("asset_type", "option")

    # A close for this position may already be working — don't stack another
    # liquidation on top (the retry 422s while the first order holds the qty).
    if has_pending_inflight_close(conn, position_id, asset):
        insert_decision(conn, ticket_id, "SKIPPED",
                        f"FORCE_CLOSE: close already in flight for {asset} — resolver owns it")
        return
    try:
        pos_side = "buy" if position.get("direction") == "short" else "sell"
        if any(o for o in get_open_orders(asset) if o.get("side") == pos_side):
            insert_decision(conn, ticket_id, "SKIPPED",
                            f"FORCE_CLOSE: an open {pos_side} order already exists at Alpaca "
                            f"for {asset} — not stacking a second close")
            log_system_event(conn, "WARNING", "execution_engine",
                             f"FORCE_CLOSE skipped — untracked open {pos_side} order exists for {asset}",
                             {"ticket_id": ticket_id, "position_id": position_id})
            return
    except Exception as exc:
        logger.warning("Open-order check failed for %s (%s) — proceeding with liquidation", asset, exc)

    # Pre-flight state check: DELETE /positions/{symbol} has no
    # client-supplied idempotency key (unlike the raw POST /orders endpoint
    # used everywhere else in this file), so it cannot be made fully
    # idempotent the same way. Checking live broker state FIRST at least
    # means a crash-and-retry against an already-closed position never
    # even attempts the DELETE call.
    try:
        alpaca_get(f"/positions/{asset}")
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 404:
            close_position(conn, position_id, 0.0, 0.0, "already_closed_at_broker")
            transition_position_state(conn, position_id, "CLOSED",
                                      "force_close pre-flight: no position at broker", "execution_engine")
            insert_decision(conn, ticket_id, "SKIPPED",
                            f"FORCE_CLOSE: pre-flight check found no position for {asset} — already closed")
            record_force_close_outcome(conn, position_id, success=True)
            record_execution_attempt(conn, ticket_id, "preflight_check", "rejected",
                                     error_state="404", payload={"asset": asset})
            logger.info("Ticket %s: pre-flight check found %s already closed — skipped DELETE", ticket_id, asset)
            return
        # Any other pre-flight failure (network blip, 5xx) — fall through
        # to the real attempt below and let its own classification handle it.

    exit_bid, exit_ask = get_latest_quote(asset, asset_type)
    expected_price = (exit_bid + exit_ask) / 2.0 if (exit_bid and exit_ask) else None

    try:
        order = alpaca_delete(f"/positions/{asset}")
    except Exception as exc:
        already_closed, terminal, class_reason = _classify_force_close_failure(asset, asset_type, exc)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)

        if already_closed:
            fallback_price = expected_price or 0.0
            close_position(conn, position_id, fallback_price, 0.0, "already_closed_at_broker",
                           exit_bid=exit_bid, exit_ask=exit_ask)
            transition_position_state(conn, position_id, "CLOSED",
                                      f"force_close: {class_reason}", "execution_engine")
            insert_decision(conn, ticket_id, "SKIPPED", f"FORCE_CLOSE: {class_reason}")
            record_force_close_outcome(conn, position_id, success=True)
            record_execution_attempt(conn, ticket_id, "force_close", "rejected",
                                     error_state=str(status_code or "already_closed"),
                                     payload={"asset": asset, "reason": class_reason})
            logger.info("Ticket %s: %s — ledger closed to match", ticket_id, class_reason)
            return

        reason = f"FORCE_CLOSE: liquidation order failed for {asset}: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR" if not terminal else "WARNING", "execution_engine", reason,
                         {"ticket_id": ticket_id, "terminal": terminal})
        record_force_close_outcome(conn, position_id, success=False, error=str(exc),
                                   terminal=terminal, terminal_reason=class_reason if terminal else None)
        record_execution_attempt(conn, ticket_id, "force_close", "error",
                                 error_state=str(status_code or type(exc).__name__),
                                 payload={"asset": asset, "terminal": terminal})
        return

    alpaca_order_id = order.get("id", "")
    record_execution_attempt(conn, ticket_id, "force_close", "accepted",
                             broker_order_id=alpaca_order_id, payload={"asset": asset})
    try:
        # Return value MUST be checked: the liquidation DELETE above has
        # already reached the broker for real by this point, so if we've
        # lost the claim, marking this FAILED would be actively wrong —
        # record UNKNOWN instead. reconcile_unknown_force_close() resolves
        # it against live broker state on the next attempt.
        if not renew_ticket_claim(conn, ticket_id, WORKER_ID):
            record_force_close_unknown(conn, position_id, asset, ticket_id, WORKER_ID)
            # An UNKNOWN decision (not silence, unlike Path 1/2's
            # ClaimLostError handling) is written for THIS ticket so
            # has_pending_force_close_ticket() no longer sees it as
            # outstanding — otherwise risk_agent.py could never schedule
            # another attempt, and reconciliation would never get a
            # ticket to run against.
            insert_decision(conn, ticket_id,
                            "UNKNOWN", "Claim lost after liquidation order was already submitted")
            raise ClaimLostError(
                f"Lost claim ownership for ticket {ticket_id} after FORCE_CLOSE liquidation "
                f"order was already submitted — another worker may already be processing it. "
                f"Outcome recorded as UNKNOWN for position {position_id}."
            )
        filled_order = poll_order_until_filled(alpaca_order_id) if alpaca_order_id else order
    except ClaimLostError:
        raise  # propagate untouched — see ClaimLostError's docstring
    except Exception as exc:
        reason = f"FORCE_CLOSE: order poll failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        record_force_close_outcome(conn, position_id, success=False, error=str(exc))
        return

    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None
    if fill_price is None:
        status = filled_order.get("status", "")
        if alpaca_order_id and status not in ORDER_TERMINAL_DEAD:
            # Liquidation order is live but hasn't filled inside the poll
            # window — hand it to the in-flight resolver rather than
            # declaring failure while the order still works the position.
            record_inflight_order(
                conn, ticket_id, alpaca_order_id, "close",
                {"symbol": asset, "asset_type": asset_type,
                 "strategy_id": payload.get("strategy_id")},
                position_id=position_id, exit_reason="hard_close",
            )
            reason = (f"FORCE_CLOSE: order {alpaca_order_id} still working "
                      f"(status={status}) — recorded in-flight")
            insert_decision(conn, ticket_id, "SUBMITTED", reason)
            log_system_event(conn, "INFO", "execution_engine", reason,
                             {"ticket_id": ticket_id, "position_id": position_id})
            return
        reason = f"FORCE_CLOSE: no fill price — status={status}"
        logger.warning("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {
            "ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id,
        })
        record_force_close_outcome(conn, position_id, success=False,
                                   error=f"no fill price, status={status}")
        return

    slippage = abs(fill_price - expected_price) / expected_price if expected_price and expected_price > 0 else 0.0
    actually_closed = close_position(conn, position_id, fill_price, slippage, "hard_close",
                                     exit_bid=exit_bid, exit_ask=exit_ask)
    if actually_closed:
        transition_position_state(conn, position_id, "CLOSED", "force_close: filled", "execution_engine")
    record_force_close_outcome(conn, position_id, success=True)
    record_execution_attempt(conn, ticket_id, "force_close", "accepted",
                             broker_order_id=alpaca_order_id, fill_state="filled",
                             payload={"asset": asset, "fill_price": fill_price})

    if not actually_closed:
        # A real order filled at the broker just now, but the ledger row
        # was already 'closed' — a second FORCE_CLOSE ticket for the same
        # position beat this one to it. Genuine double-execution at the
        # broker, not just a ledger race — worth its own log line.
        reasoning = (
            f"FORCE_CLOSE order filled at {fill_price:.4f} but ledger position "
            f"{position_id} was already closed by another attempt — exit data NOT "
            f"overwritten; a duplicate closing order reached the broker"
        )
        insert_decision(conn, ticket_id, "EXECUTED_DUPLICATE", reasoning)
        log_system_event(conn, "WARNING", "execution_engine", reasoning,
                         {"ticket_id": ticket_id, "position_id": position_id,
                          "alpaca_order_id": alpaca_order_id, "fill_price": fill_price})
        logger.warning("Ticket %s: %s", ticket_id, reasoning)
        return

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
    """
    qty = int(payload.get("qty", 1))
    spread_group_id = str(uuid.uuid4())
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


def record_entry_fill(conn, ticket_id: str, payload: dict, filled_order: dict,
                      alpaca_order_id: str, expected_price: float = None,
                      entry_bid: float = None, entry_ask: float = None,
                      note: str = "") -> None:
    """
    A filled entry order becomes ledger row(s) + an EXECUTED decision.
    Shared by the immediate-fill path (process_ticket) and the in-flight
    resolver — the ledger write must be identical regardless of when the
    fill was observed.
    """
    asset_type = payload["asset_type"]
    symbol = payload["symbol"]
    legs = payload.get("legs") or []
    direction = payload.get("direction", "long")
    sized_notional = float(payload.get("sized_notional", 0))

    if asset_type == "option" and legs:
        insert_spread_ledger_rows(conn, ticket_id, payload, filled_order, alpaca_order_id)
        return

    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None
    if fill_price is None or fill_price <= 0:
        raise RuntimeError(f"order {alpaca_order_id} filled without a fill price")

    filled_qty_str = filled_order.get("filled_qty")
    filled_qty = float(filled_qty_str) if filled_qty_str else float(payload.get("qty", 1))

    instrument = payload.get("option_symbol", symbol) if asset_type == "option" else symbol
    if entry_bid is None and entry_ask is None:
        entry_bid, entry_ask = get_latest_quote(instrument, asset_type)

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
        opening_qty = filled_qty
        reduced_position_ids = []
    else:
        ledger_direction = direction
        delta_exposure = 1.0 if direction == "long" else -1.0

        # Broker-aware position intent validation (Finding: 2026-07-22 AAPL
        # incident). Never trust `direction` alone to mean "this is a new
        # position" — check what the broker actually held BEFORE this order
        # went in. A sell against an existing broker long (from ANY
        # bot_source, including UNATTRIBUTED) reduces that exposure; it is
        # only a genuine new short for whatever quantity exceeds the
        # existing long. Symmetric for buy vs. an existing short.
        side = "buy" if direction == "long" else "sell"
        broker_pos_before = payload.get("broker_position_before_entry")
        classification = classify_equity_exposure(broker_pos_before, side, filled_qty)
        opening_qty = classification["opening_qty"]
        reduced_position_ids = []

        if classification["reduces_existing"]:
            opposing_direction = "long" if side == "sell" else "short"
            reduced_position_ids = reduce_opposing_equity_rows(
                conn, instrument, opposing_direction, classification["reducing_qty"],
                fill_price, "reconciled_broker_exposure_merge",
                note=f"ticket {ticket_id} ({payload.get('bot_source')}) fill of "
                     f"{filled_qty:g} {side} merged into pre-existing broker exposure",
            )
            log_system_event(
                conn, "WARNING", "execution_engine",
                f"{instrument}: {classification['reducing_qty']:g} of this "
                f"{side} fill reduced pre-existing broker exposure instead of opening a "
                f"new {direction} position — not attributed to a single bot at entry, "
                f"needs reconciliation review",
                {"ticket_id": ticket_id, "symbol": instrument, "side": side,
                 "reduced_qty": classification["reducing_qty"], "opening_qty": opening_qty,
                 "reduced_position_ids": reduced_position_ids},
            )

        if opening_qty <= 1e-9:
            # The entire fill reduced existing exposure — no new directional
            # position exists at all. Recording one here would recreate
            # exactly the AAPL bug (a phantom short/long with nothing real
            # behind it).
            reasoning = (
                f"Filled at {fill_price:.4f} — entire {filled_qty:g} share fill absorbed "
                f"into pre-existing broker exposure (position_ids: {reduced_position_ids}), "
                f"no new position opened, alpaca_order_id={alpaca_order_id}"
            )
            if note:
                reasoning += f" ({note})"
            insert_decision(conn, ticket_id, "EXECUTED", reasoning)
            log_system_event(conn, "INFO", "execution_engine",
                             f"Executed {symbol} (equity) — reduced existing exposure, no new position",
                             {"ticket_id": ticket_id, "fill_price": fill_price,
                              "reduced_position_ids": reduced_position_ids})
            logger.info("Ticket %s EXECUTED: reduced existing exposure only, no new position "
                        "(fill=%.4f)", ticket_id, fill_price)
            return

    # notional_risk scales with whatever fraction of the fill actually opens
    # new exposure — the reduced portion's risk belongs to the position(s)
    # it reduced, not to a freshly-created row.
    opening_notional = sized_notional * (opening_qty / filled_qty) if filled_qty > 0 else sized_notional

    ledger_data = {
        "bot_source": payload["bot_source"],
        "strategy_id": payload.get("strategy_id"),
        "asset": instrument,
        "asset_type": asset_type,
        "direction": ledger_direction,
        "delta_exposure": delta_exposure,
        "notional_risk": opening_notional,
        "qty": opening_qty,
        "entry_price": fill_price,
        "entry_time": datetime.now(timezone.utc),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "alpaca_order_id": alpaca_order_id,
        "stop_pct": payload.get("stop_pct"),
        "target_pct": payload.get("target_pct"),
    }

    position_id = insert_position(conn, ledger_data)

    reasoning = (f"Filled at {fill_price:.4f}, slippage={slippage:.4f}, "
                 f"position_id={position_id}, alpaca_order_id={alpaca_order_id}")
    if reduced_position_ids:
        reasoning += (f" ({classification['reducing_qty']:g} of {filled_qty:g} filled qty "
                      f"reduced existing positions {reduced_position_ids}; "
                      f"{opening_qty:g} opened as new)")
    if note:
        reasoning += f" ({note})"
    insert_decision(conn, ticket_id, "EXECUTED", reasoning)
    log_system_event(conn, "INFO", "execution_engine",
                     f"Executed {symbol} ({asset_type}) — {direction}",
                     {"ticket_id": ticket_id, "position_id": position_id,
                      "fill_price": fill_price, "slippage": slippage,
                      "strategy_id": payload.get("strategy_id")})
    logger.info("Ticket %s EXECUTED: position_id=%s fill=%.4f slippage=%.4f",
                ticket_id, position_id, fill_price, slippage)


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

    # Snapshot the broker's position BEFORE this order — the source of
    # truth for whether the fill (once it happens) opens genuinely new
    # exposure or merely reduces/closes exposure some other ledger row
    # already represents (see classify_equity_exposure). Must be taken now,
    # not at fill-resolution time, since the in-flight resolver may run
    # cycles later after other trades have changed the broker's position.
    if asset_type == "equity":
        try:
            payload["broker_position_before_entry"] = get_alpaca_position(symbol)
        except Exception as exc:
            logger.warning("Ticket %s: could not snapshot broker position for %s before "
                           "entry (%s) — proceeding without cross-attribution protection",
                           ticket_id, symbol, exc)
            payload["broker_position_before_entry"] = None

    client_order_id = client_order_id_for(ticket_id)
    record_client_order_id(conn, ticket_id, client_order_id)  # submission begins now
    existing_order = find_order_by_client_order_id(client_order_id)
    if existing_order:
        # A process crash between "Alpaca accepted this order" and "we
        # recorded that anywhere" (including as an in-flight row) is the
        # one class of duplicate-order risk neither claim/lease sequencing
        # nor in-flight tracking alone can fully close — recover the
        # original order instead of ever attempting to resubmit it.
        logger.warning(
            "Ticket %s: order with client_order_id=%s already exists at broker "
            "(id=%s, status=%s) — recovering instead of resubmitting",
            ticket_id, client_order_id, existing_order.get("id"), existing_order.get("status"),
        )

    try:
        if asset_type == "equity":
            entry_bid, entry_ask = get_latest_quote(symbol, "equity")
            order = existing_order or place_equity_order(payload, client_order_id)
            expected_price = get_current_price(symbol)
        elif asset_type == "option":
            if not legs:
                option_symbol = payload.get("option_symbol", symbol)
                entry_bid, entry_ask = get_latest_quote(option_symbol, "option")
                if entry_bid and entry_ask:
                    expected_price = (entry_bid + entry_ask) / 2.0
            order = existing_order or place_options_order(payload, client_order_id)
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

    # From this point on the order EXISTS at Alpaca. Every path below must
    # end in exactly one of: EXECUTED (fill recorded), FAILED (order verified
    # dead), or SUBMITTED + nwt_inflight_orders row (order still live, the
    # resolver owns it now). Marking FAILED while the order is still working
    # is how the 2026-07-16 untracked-position incident happened.
    #
    # The claim lease MUST be renewed here and its result checked: the order
    # above has already reached the broker for real, so if we've lost the
    # claim (another worker's lease outlived ours and reclaimed it), that
    # worker may be concurrently processing the same ticket — raising
    # ClaimLostError (rather than treating this as an ordinary poll
    # failure) is what stops us from also writing a decision/inflight row
    # that could race with theirs.
    if not renew_ticket_claim(conn, ticket_id, WORKER_ID):
        raise ClaimLostError(
            f"Lost claim ownership for ticket {ticket_id} after order {alpaca_order_id} "
            f"was already submitted — another worker may already be processing it"
        )

    try:
        filled_order = poll_order_until_filled(alpaca_order_id)
    except Exception as exc:
        record_inflight_order(conn, ticket_id, alpaca_order_id, "entry", payload)
        reason = (f"Order poll failed ({exc}) — order {alpaca_order_id} recorded "
                  f"in-flight for resolution next cycle")
        insert_decision(conn, ticket_id, "SUBMITTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    fill_status = filled_order.get("status", "")
    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None

    if fill_status in ORDER_TERMINAL_DEAD:
        filled_qty = float(filled_order.get("filled_qty") or 0)
        if filled_qty > 0 and fill_price:
            # Partially filled before dying — those shares/contracts are a
            # real position and MUST hit the ledger.
            try:
                record_entry_fill(conn, ticket_id, payload, filled_order, alpaca_order_id,
                                  expected_price=expected_price,
                                  entry_bid=entry_bid, entry_ask=entry_ask,
                                  note=f"partial fill, order {fill_status}")
            except Exception as exc:
                reason = f"Ledger insert failed on partial fill: {exc}"
                insert_decision(conn, ticket_id, "FAILED", reason)
                log_system_event(conn, "ERROR", "execution_engine", reason,
                                 {"ticket_id": ticket_id})
            return
        reason = f"Order did not fill — terminal status={fill_status}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason,
                         {"ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id})
        return

    # mleg orders report per-leg fills; the top-level price is the net debit/
    # credit and may legitimately be absent — status alone decides for spreads
    if fill_status != "filled" or (fill_price is None and not legs):
        record_inflight_order(conn, ticket_id, alpaca_order_id, "entry", payload)
        reason = (f"Order still working after poll window (status={fill_status}) — "
                  f"recorded in-flight for resolution next cycle")
        insert_decision(conn, ticket_id, "SUBMITTED", reason)
        log_system_event(conn, "INFO", "execution_engine", reason,
                         {"ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id})
        logger.info("Ticket %s SUBMITTED (in-flight): order=%s status=%s",
                    ticket_id, alpaca_order_id, fill_status)
        return

    try:
        record_entry_fill(conn, ticket_id, payload, filled_order, alpaca_order_id,
                          expected_price=expected_price,
                          entry_bid=entry_bid, entry_ask=entry_ask)
    except Exception as exc:
        reason = f"Ledger insert failed: {exc}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Execution Engine starting")

    conn = get_db()
    try:
        upsert_heartbeat(conn)

        # In-flight orders are resolved EVERY run, no_trade_mode or not —
        # these orders already exist at Alpaca; recording their fills is
        # risk accounting, not new trading. Skipping this while halted is
        # how untracked positions accumulate during an incident.
        try:
            resolve_inflight_orders(conn)
        except Exception as exc:
            logger.error("In-flight resolver error: %s", exc)
            log_system_event(conn, "ERROR", "execution_engine",
                             f"In-flight resolver failed: {exc}")

        # Closes (FORCE_CLOSE / CLOSE_REQUEST) also run while halted:
        # no_trade_mode means no NEW positions, but the Risk Agent's
        # liquidation authority must still be executable — otherwise a Rule
        # 12 hard close for an expiring option piles up unexecuted for
        # exactly as long as the halt lasts (2026-07-16: AAPL 260717 call).
        close_tickets = fetch_force_close_tickets(conn)
        if close_tickets:
            logger.info("Found %d force-close tickets", len(close_tickets))
            for ticket in close_tickets:
                ticket_id = str(ticket.get("ticket_id"))
                # Atomic claim before any slow Alpaca call — see
                # db/migrate_2026_07_execution_safety.sql. crontab.txt has no
                # overlap guard, so without this a second concurrent engine.py
                # run could select and process this same ticket again,
                # placing a second real closing order at the broker.
                if not claim_ticket(conn, ticket_id, WORKER_ID):
                    logger.info("Ticket %s: not claimed (already owned by another worker) — skipping", ticket_id)
                    continue
                try:
                    if ticket.get("type") == "FORCE_CLOSE":
                        process_force_close(conn, ticket)
                    else:
                        process_close_ticket(conn, ticket)
                    release_ticket_claim(conn, ticket_id, WORKER_ID, status="done")
                except ClaimLostError as exc:
                    # We no longer own this ticket, and a real broker order
                    # may already have been placed before we found out — do
                    # NOT write a decision (would tell the system "nothing
                    # happened" when something might have) and do NOT
                    # release (the ownership guard makes it a no-op anyway).
                    conn.rollback()
                    logger.critical("Ticket %s: %s", ticket_id, exc)
                    log_system_event(conn, "CRITICAL", "execution_engine", str(exc), {"ticket_id": ticket_id})
                except Exception as exc:
                    logger.error("Unhandled error in close ticket %s: %s", ticket_id, exc)
                    release_ticket_claim(conn, ticket_id, WORKER_ID, status="failed")

        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.warning("no_trade_mode is SET: %s — closes/in-flight processed, "
                           "no new entries, monitor skipped", halt_reason)
            log_system_event(conn, "WARNING", "execution_engine",
                             f"no_trade_mode: engine ran close-only cycle: {halt_reason}")
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

        pending = fetch_pending_tickets(conn)
        logger.info("Found %d pending TRADE_REQUEST tickets", len(pending))

        if not pending:
            logger.info("No pending tickets — exiting")
            return

        # New entries only go out while the market is open. A market order
        # placed into a closed market (Track A executors run hours before
        # the US open) cannot fill inside the poll window; deferring the
        # ticket to a later cycle costs 5 minutes and keeps every order
        # observable end-to-end. Tickets stay pending — no decision written.
        if not market_is_open():
            logger.info("Market closed — deferring %d pending entry ticket(s) to a later cycle",
                        len(pending))
            return

        for ticket in pending:
            ticket_id = str(ticket.get("ticket_id", "unknown"))
            if not claim_ticket(conn, ticket_id, WORKER_ID):
                logger.info("Ticket %s: not claimed (already owned by another worker) — skipping", ticket_id)
                continue
            try:
                process_ticket(conn, ticket, directives)
                release_ticket_claim(conn, ticket_id, WORKER_ID, status="done")
            except ClaimLostError as exc:
                # See the identical handling in the FORCE_CLOSE/CLOSE_REQUEST
                # loop above — no decision, no release, CRITICAL log only.
                conn.rollback()
                logger.critical("Ticket %s: %s", ticket_id, exc)
                log_system_event(conn, "CRITICAL", "execution_engine", str(exc), {"ticket_id": ticket_id})
            except Exception as exc:
                logger.error("Unhandled error on ticket %s: %s", ticket_id, exc)
                release_ticket_claim(conn, ticket_id, WORKER_ID, status="failed")
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
