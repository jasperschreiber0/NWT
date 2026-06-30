"""
nwt_agents/shared_context.py
Single import module providing: regime, conviction data, sizing, and control helpers to all agents.
All agents must import from here — never duplicate these lookups.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

# Shared timing constants — single definition for risk agent AND execution path
NEW_ENTRY_CUTOFF_UTC_HOUR = 19    # No new entries after 19:30 UTC (15:30 EDT)
NEW_ENTRY_CUTOFF_UTC_MINUTE = 30
TRACK_COOLOFF_HOURS = 24


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(os.environ["NWT_DB_DSN"])


# ---------------------------------------------------------------------------
# File loaders
# ---------------------------------------------------------------------------

def _shared_dir() -> Path:
    return Path(os.environ.get("SHARED_DIR", Path(__file__).parent.parent / "shared"))


def _agents_dir() -> Path:
    return Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))


def load_master_directives() -> dict:
    path = _shared_dir() / "master-directives.json"
    with open(path) as f:
        return json.load(f)


def load_conviction_tickets() -> list:
    path = _agents_dir() / "conviction_tickets.json"
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def load_conviction_summary() -> str:
    path = _agents_dir() / "conviction_summary.txt"
    if not path.exists():
        return ""
    return path.read_text()


def load_layer0_data() -> dict:
    path = _agents_dir() / "layer0_data.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Strategy genome — CRITICAL: every agent queries at startup, never hardcodes
# ---------------------------------------------------------------------------

def get_strategy_genome(conn, strategy_id: str) -> dict:
    """
    Query nwt_strategy_genome for the given strategy_id.
    Raises RuntimeError if not found or inactive — caller must NOT proceed.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_strategy_genome WHERE strategy_id = %s AND active = TRUE",
            (strategy_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"No active genome row found for {strategy_id} — refusing to run")
    return dict(row)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def compute_final_sizing(directives: dict, base_notional: float, bot_key: str) -> float:
    """
    Apply capital_weight and size_cap from master-directives bot_permissions,
    then apply regime confidence and transition_risk multipliers.
    """
    perms = directives.get("bot_permissions", {}).get(bot_key, {})
    capital_weight = perms.get("capital_weight", 0.0)
    size_cap = perms.get("size_cap", 0.0)

    regime = directives.get("regime", {})
    multiplier = 1.0
    if regime.get("confidence", 1.0) < 0.5:
        multiplier *= 0.7
    if regime.get("transition_risk", 0.0) > 0.5:
        multiplier *= 0.5

    return base_notional * capital_weight * size_cap * multiplier


# ---------------------------------------------------------------------------
# no_trade_mode — checked by every trading agent before doing anything
# ---------------------------------------------------------------------------

def check_no_trade_mode(conn) -> tuple:
    """
    Returns (is_halted, reason).
    If is_halted is True the caller must log and exit immediately.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value, reason FROM nwt_system_flags WHERE flag = 'no_trade_mode'",
        )
        row = cur.fetchone()
    if row and row[0]:
        return True, row[1] or "no_trade_mode flag is set"
    return False, ""


def set_no_trade_mode(conn, reason: str, set_by: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_system_flags (flag, value, reason, set_by, updated_at)
            VALUES ('no_trade_mode', TRUE, %s, %s, NOW())
            ON CONFLICT (flag) DO UPDATE
              SET value=TRUE, reason=%s, set_by=%s, updated_at=NOW()
            """,
            (reason, set_by, reason, set_by),
        )
    conn.commit()
    try:
        from notifier import alert_no_trade_mode
        alert_no_trade_mode(reason)
    except Exception:
        pass


def clear_no_trade_mode(conn, cleared_by: str) -> None:
    """Only called after human-acknowledged recon pass or manual override."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_system_flags
            SET value=FALSE, reason=NULL, set_by=%s, updated_at=NOW()
            WHERE flag='no_trade_mode'
            """,
            (cleared_by,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Synchronous risk vetoes — checked IN the order path, not just by the
# risk agent's 5-minute sweep. Risk enforcement must fire before orders
# reach Alpaca; these are the authoritative state flags re-checked at
# submission time.
# ---------------------------------------------------------------------------

def get_disabled_tracks(conn) -> set:
    """Tracks disabled by the risk agent (cooling-off) in the last 24h."""
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
    for row in rows:
        payload = row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if isinstance(payload, dict) and payload.get("track"):
            disabled.add(payload["track"])
    return disabled


def past_new_entry_cutoff(now_utc: datetime = None) -> bool:
    now_utc = now_utc or datetime.now(timezone.utc)
    return (
        now_utc.hour > NEW_ENTRY_CUTOFF_UTC_HOUR
        or (now_utc.hour == NEW_ENTRY_CUTOFF_UTC_HOUR and now_utc.minute >= NEW_ENTRY_CUTOFF_UTC_MINUTE)
    )


def pre_trade_veto(conn, track: str) -> tuple:
    """
    Final synchronous gate before submitting an order for a NEW position.
    Re-reads master-directives.json fresh (kill switch may have been activated
    since this process started) and re-checks track cooling-off and the
    new-entry cutoff. Returns (vetoed: bool, reason: str).
    Closes/liquidations must NOT go through this gate.
    """
    try:
        directives = load_master_directives()
    except FileNotFoundError:
        return True, "pre_trade_veto: master-directives.json missing — NO-TRADE MODE"

    if directives.get("global_kill_switch", False):
        return True, "pre_trade_veto: global kill switch active"

    if track and track in get_disabled_tracks(conn):
        return True, f"pre_trade_veto: track {track} in cooling-off period"

    if past_new_entry_cutoff():
        return True, (
            f"pre_trade_veto: past new-entry cutoff "
            f"{NEW_ENTRY_CUTOFF_UTC_HOUR}:{NEW_ENTRY_CUTOFF_UTC_MINUTE:02d} UTC"
        )

    return False, ""


# ---------------------------------------------------------------------------
# Inactivity — first-class state, logged as typed ticket
# ---------------------------------------------------------------------------

_INACTIVITY_CLASS_MAP = {
    "NO_CONVICTION_MATCH": "no_edge",
    "NO_SYMBOL_MATCH": "no_edge",
    "CONVICTION_BELOW_THRESHOLD": "no_edge",
    "NO_DIRECTIONAL_STRATEGY_AVAILABLE": "no_edge",
    "INSUFFICIENT_QUANT_EDGE": "no_edge",
    "ZERO_SIZING": "regime_skip",
    "SHADOW_MODE": "regime_skip",
    "GLOBAL_KILL_SWITCH": "regime_skip",
    "NO_TRADE_MODE": "regime_skip",
    "REGIME_MISMATCH": "regime_skip",
    "ARCHETYPE_CONSOLIDATED": "no_edge",
}


def log_inactivity(conn, strategy_id: str, track: str, reason: str, regime: dict) -> None:
    """
    Log inactivity as a first-class typed ticket in nwt_tickets AND a row in
    nwt_inactivity_log. Both are needed: nwt_tickets carries the full payload
    (queried by cost_agent), nwt_inactivity_log is the lightweight table queried
    by session_scorecard.check_activity_logged.
    signal_missed is assigned only by the Learning Agent — never self-reported.
    """
    inactivity_class = _INACTIVITY_CLASS_MAP.get(reason, "no_edge")
    payload = {
        "class": inactivity_class,
        "bot": track,
        "strategy_id": strategy_id,
        "reason": reason,
        "regime": regime,
    }
    try:
        insert_ticket(conn, f"TRACK_{track}", "SYSTEM", "inactivity", payload)
    except Exception as exc:
        log_system_event(conn, "WARNING", f"track_{track.lower()}",
                         f"log_inactivity ticket insert failed for {strategy_id}: {exc}", payload)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_inactivity_log (strategy_id, track, reason, regime_at_decision)
                VALUES (%s, %s, %s, %s)
                """,
                (strategy_id, track, reason, json.dumps(regime)),
            )
        conn.commit()
    except Exception as exc:
        log_system_event(conn, "WARNING", f"track_{track.lower()}",
                         f"log_inactivity nwt_inactivity_log insert failed for {strategy_id}: {exc}")


# ---------------------------------------------------------------------------
# System log
# ---------------------------------------------------------------------------

def log_system_event(
    conn,
    level: str,
    component: str,
    message: str,
    payload: dict = None,
) -> None:
    """INSERT a row into nwt_system_log."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_system_log (level, component, message, payload) VALUES (%s, %s, %s, %s)",
            (level, component, message, json.dumps(payload) if payload is not None else None),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Ticket helpers — nwt_tickets is append-only, always INSERT to nwt_ticket_decisions
# ---------------------------------------------------------------------------

def insert_ticket(
    conn,
    from_agent: str,
    to_agent: str,
    type_: str,
    payload: dict,
) -> str:
    """INSERT into nwt_tickets. Returns ticket_id as string."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_tickets (from_agent, to_agent, type, payload)
            VALUES (%s, %s, %s, %s)
            RETURNING ticket_id
            """,
            (from_agent, to_agent, type_, json.dumps(payload)),
        )
        ticket_id = cur.fetchone()[0]
    conn.commit()
    return str(ticket_id)


def insert_decision(
    conn,
    ticket_id: str,
    decision: str,
    reasoning: str,
    decided_by: str,
) -> None:
    """INSERT into nwt_ticket_decisions. NEVER UPDATE nwt_tickets."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_decisions (ticket_id, decision, reasoning, decided_by)
            VALUES (%s, %s, %s, %s)
            """,
            (ticket_id, decision, reasoning, decided_by),
        )
    conn.commit()
