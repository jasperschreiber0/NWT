"""
nwt_agents/shared_context.py
Single import module providing: regime, conviction data, sizing, and control helpers to all agents.
All agents must import from here — never duplicate these lookups.
"""

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

# Shared timing constants
TRACK_COOLOFF_HOURS = 24

# New-entry cutoff is 15:30 ET, computed DST-aware below — never a fixed UTC
# hour/minute. A fixed "19:30 UTC" constant is only correct during EDT
# (summer); in EST (winter) 15:30 ET is 20:30 UTC, an hour later, so a fixed
# constant silently vetoes valid, already-approved trades for a third of the
# year. risk_agent.py's own Rule 0 already computes this correctly — this is
# the same logic, exposed here so every other caller (pre_trade_veto included)
# uses the one true implementation instead of a second, driftable copy.
ET_TZ = ZoneInfo("America/New_York")


def new_entry_cutoff_utc(now: datetime = None) -> datetime:
    """15:30 ET in UTC, fully DST-aware, for the date `now` (or today) falls on."""
    et_now = (now or datetime.now(timezone.utc)).astimezone(ET_TZ)
    cutoff = datetime(et_now.year, et_now.month, et_now.day, 15, 30, tzinfo=ET_TZ)
    return cutoff.astimezone(timezone.utc)


def option_dte(option_symbol: str) -> int | None:
    """
    Days to expiry, parsed from the OCC symbol (ROOT + YYMMDD + C/P + strike*1000).
    Returns None if the symbol can't be parsed.
    """
    key = (option_symbol or "").upper().strip()
    if len(key) < 15:
        return None
    try:
        expiry = datetime.strptime(key[-15:-9], "%y%m%d").date()
    except ValueError:
        return None
    today = datetime.now(ET_TZ).date()
    return (expiry - today).days


def clean_alpaca_base_url(url: str) -> str:
    """
    Strip a trailing slash AND a trailing /v2, if present.

    CLAUDE.md's own documented gotcha: NWT_ALPACA_BASE_URL / NWT_ALPACA_DATA_URL
    must not carry a trailing /v2 — every call site appends its own /v2/...
    path, so a misconfigured env var causes a silent double /v2/v2/ -> 404 on
    every request. Every module reading these env vars must route through
    this instead of a bare .rstrip("/").
    """
    url = (url or "").rstrip("/")
    if url.lower().endswith("/v2"):
        url = url[:-len("/v2")]
    return url


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
    """
    shared/master-directives.json is live runtime state, gitignored so a
    stash/checkout/reset on the server can never silently revert it (this is
    exactly how the 2026-06-29/07-01 kill-switch outage happened — the file
    used to be tracked, carrying a cold-start placeholder with
    global_kill_switch=true, and a stash reverted live state back to it for
    three sessions). A fresh checkout therefore has no live file at all —
    bootstrap it once from the committed .example template. Never overwrites
    an existing file.
    """
    path = _shared_dir() / "master-directives.json"
    if not path.exists():
        example = _shared_dir() / "master-directives.json.example"
        if example.exists():
            path.write_text(example.read_text())
    with open(path) as f:
        return json.load(f)


def load_conviction_tickets() -> list:
    path = _agents_dir() / "conviction_tickets.json"
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


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


def get_active_strategy_ids(conn, track: str) -> list:
    """
    Active strategy_ids for a track, e.g. track='C' -> ['C1', 'C2', ...].
    Replaces hardcoded range(1, 13) loops in track_c/d/e.py so deactivating
    a strategy (decay retirement, strategy focus migrations) doesn't produce
    spurious genome_missing faults for strategies that were deliberately
    turned off.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_id FROM nwt_strategy_genome WHERE track = %s AND active = TRUE ORDER BY strategy_id",
            (track,),
        )
        return [row[0] for row in cur.fetchall()]


def get_shadow_genome(conn, strategy_id: str) -> dict:
    """
    Return the pending shadow-mutation candidate version for a strategy_id,
    if one exists (shadow_mode=TRUE, active=FALSE, highest version). Returns
    {} if none — a strategy with no shadow candidate is the normal case.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM nwt_strategy_genome
            WHERE strategy_id = %s AND shadow_mode = TRUE AND active = FALSE
            ORDER BY version DESC LIMIT 1
            """,
            (strategy_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else {}


def is_mutation_frozen(conn) -> bool:
    """Risk Agent authority: freeze mutation promotion (never proposal)."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM nwt_system_flags WHERE flag = 'mutation_frozen'")
        row = cur.fetchone()
    return bool(row and row[0])


def set_mutation_frozen(conn, frozen: bool, reason: str, set_by: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_system_flags (flag, value, reason, set_by, updated_at)
            VALUES ('mutation_frozen', %s, %s, %s, NOW())
            ON CONFLICT (flag) DO UPDATE SET value=%s, reason=%s, set_by=%s, updated_at=NOW()
            """,
            (frozen, reason, set_by, frozen, reason, set_by),
        )
    conn.commit()


def upsert_agent_state(conn, agent: str, status: str, detail: dict = None) -> None:
    """Generic per-agent status row, surfaced by the dashboard's /api/performance."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_agent_state (agent, status, detail, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (agent) DO UPDATE SET status=%s, detail=%s, updated_at=NOW()
                """,
                (agent, status, json.dumps(detail) if detail is not None else None,
                 status, json.dumps(detail) if detail is not None else None),
            )
        conn.commit()
    except Exception:
        pass  # status surface must never break the calling agent


def regime_matches(genome_regime: str, current_regime: dict) -> bool:
    """
    Check if a genome row's regime string matches the current primary or
    secondary regime. Single implementation — track_c/d/e.py each used to
    carry an identical copy of this; a future fix to one and not the others
    would silently fork regime-matching behavior across tracks.
    """
    primary = (current_regime.get("primary_regime") or "").lower()
    secondary = (current_regime.get("secondary_regime") or "").lower()
    genome_r = (genome_regime or "").lower()
    if genome_r in ("any", "", "all"):
        return True
    return genome_r == primary or genome_r == secondary


# ---------------------------------------------------------------------------
# VIX proxy — single source of truth
# ---------------------------------------------------------------------------

def fetch_vix_proxy(alpaca_base_url: str, alpaca_data_url: str, alpaca_headers: dict,
                     directives: dict = None) -> tuple:
    """
    Single implementation of a VIX-equivalent volatility reading, shared by
    every agent that needs one. Returns (value, source) or (None, "unavailable").
    VIX==0 is treated as missing, never as a signal.

    Prefers master-directives.json's own `vix` field (set daily by Portfolio
    Brain). Falls back to computing ATM SPY ~30 DTE implied volatility
    directly from Alpaca's options chain when directives are stale/absent.

    Deliberately never returns a raw VIXY (VIX futures ETF) price as if it
    were the index: VIXY trades ~$12-18 in ordinary conditions because it
    tracks front-month futures with contango roll decay, not spot VIX — a
    caller comparing that price directly against a `> 40` kill-switch
    threshold would find the check structurally unreachable. Two call sites
    (layer0_builder.py's prescreener hard filter and risk_agent.py's kill
    switch) used to compute two different, disagreeing "VIX" values; this
    is the one both now share.
    """
    if directives:
        vix = directives.get("vix") or 0.0
        if vix > 0:
            return float(vix), "master_directives"

    try:
        today = date.today()
        url = f"{alpaca_base_url}/v2/options/contracts"
        params = {
            "underlying_symbols": "SPY",
            "expiration_date_gte": (today + timedelta(days=25)).isoformat(),
            "expiration_date_lte": (today + timedelta(days=35)).isoformat(),
            "type": "call",
            "limit": 10,
        }
        resp = requests.get(url, headers=alpaca_headers, params=params, timeout=15)
        resp.raise_for_status()
        contracts = resp.json().get("option_contracts", [])
        if contracts:
            price_url = f"{alpaca_data_url}/v2/stocks/SPY/trades/latest"
            pr = requests.get(price_url, headers=alpaca_headers, timeout=10)
            pr.raise_for_status()
            spy_price = float(pr.json()["trade"]["p"])
            atm = min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - spy_price))
            iv = float(atm.get("implied_volatility") or 0)
            if iv > 0:
                return iv * 100, "spy_iv_proxy"
    except Exception:
        pass

    return None, "unavailable"


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
    return now_utc >= new_entry_cutoff_utc(now_utc)


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
            f"{new_entry_cutoff_utc().strftime('%H:%M')} UTC (15:30 ET)"
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
    Log inactivity as a first-class typed ticket in nwt_tickets.
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


# ---------------------------------------------------------------------------
# Shadow candidate logging — nwt_decision_inputs
# Every strategy eligible to propose a trade gets logged here, win or lose
# the archetype-consolidation pick, so the Learning Agent can eventually
# score signal quality on candidates that never got a real ticket. This is
# separate from log_inactivity: log_inactivity is the operational ticket
# audit trail; nwt_decision_inputs is the analytical dataset shadow_decision_
# evaluator.py later fills in with would_have_won / shadow_pnl_pct.
# ---------------------------------------------------------------------------

def log_decision_input(
    conn,
    run_date,
    symbol: str,
    strategy_id: str,
    track: str,
    regime: dict,
    conviction_score: float,
    archetype: str,
    is_winner: bool,
    decision: str,
    direction: str = None,
    rejection_reason: str = None,
    entry_price_ref: float = None,
    target_pct: float = None,
    stop_pct: float = None,
    dte_target: int = None,
    ticket_id: str = None,
    genome_version: int = None,
) -> None:
    """
    INSERT one row into nwt_decision_inputs for a candidate that was eligible
    to trade this run (matched regime + asset_universe + entry_threshold),
    whether or not it was the archetype winner. entry_price_ref/target_pct/
    stop_pct/dte_target are required for shadow_decision_evaluator.py to
    later compute would_have_won; leave them None if unavailable (e.g. no
    layer0 price data) rather than guessing.

    genome_version ties a row to the specific genome version it was
    evaluated against — set it when logging a Strategy Mutator shadow
    evaluation (decision='SHADOW_MUTATION') so the promotion job can group
    a mutation candidate's outcomes separately from its baseline's.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_decision_inputs
                    (run_date, symbol, strategy_id, track, regime, conviction_score,
                     archetype, is_winner, decision, direction, rejection_reason,
                     entry_price_ref, target_pct, stop_pct, dte_target, ticket_id,
                     genome_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_date, symbol, strategy_id, track, json.dumps(regime), conviction_score,
                    archetype, is_winner, decision, direction, rejection_reason,
                    entry_price_ref, target_pct, stop_pct, dte_target, ticket_id,
                    genome_version,
                ),
            )
        conn.commit()
    except Exception as exc:
        log_system_event(conn, "WARNING", f"track_{track.lower()}",
                         f"log_decision_input insert failed for {strategy_id}: {exc}")


# ---------------------------------------------------------------------------
# Strategy Mutator — shadow-mutation evaluation
# ---------------------------------------------------------------------------

def evaluate_shadow_mutation(
    conn,
    strategy_id: str,
    track: str,
    find_ticket_fn,
    conviction_tickets: list,
    current_regime: dict,
    layer0: dict,
    run_date,
) -> None:
    """
    If mutation_agent.py has proposed a shadow-mutation candidate genome
    version for this strategy, run the SAME candidate-matching function the
    caller already used for its live/baseline evaluation, but against the
    shadow genome's parameters instead. Logs to nwt_decision_inputs tagged
    with genome_version, entirely separate from the baseline's own logging —
    never produces a TRADE_PROPOSAL ticket, never touches sizing or risk.

    find_ticket_fn must have the signature (conviction_tickets, genome, regime)
    -> ticket | None, matching track_c/d/e.py's own matching functions.

    This is how a shadow mutation accumulates the same would_have_won data a
    live candidate does — shadow_decision_evaluator.py fills in
    would_have_won/shadow_pnl_pct for these rows exactly like any other, and
    mutation_agent.py's promotion pass groups by (strategy_id, genome_version)
    to check the Learning Gate.
    """
    shadow_genome = get_shadow_genome(conn, strategy_id)
    if not shadow_genome:
        return

    version = shadow_genome["version"]
    ticket = find_ticket_fn(conviction_tickets, shadow_genome, current_regime)

    if ticket is None:
        log_decision_input(
            conn, run_date=run_date, symbol=None, strategy_id=strategy_id,
            track=track, regime=current_regime, conviction_score=0,
            archetype=shadow_genome.get("archetype") or strategy_id, is_winner=False,
            decision="SHADOW_MUTATION_NO_MATCH", genome_version=version,
        )
        return

    symbol = ticket.get("symbol")
    entry_price_ref = layer0.get("symbols", {}).get(symbol, {}).get("price") or None
    dte_target = ticket.get("dte_target", shadow_genome.get("dte_min", 14))
    dte_target = max(shadow_genome["dte_min"], min(shadow_genome["dte_max"], dte_target))

    log_decision_input(
        conn, run_date=run_date, symbol=symbol, strategy_id=strategy_id,
        track=track, regime=current_regime, conviction_score=ticket.get("conviction_score", 0),
        archetype=shadow_genome.get("archetype") or strategy_id, is_winner=True,
        decision="SHADOW_MUTATION", direction=ticket.get("direction", "long"),
        entry_price_ref=entry_price_ref,
        target_pct=float(shadow_genome["profit_target_pct"]),
        stop_pct=-abs(float(shadow_genome["stop_loss_pct"])),
        dte_target=dte_target, genome_version=version,
    )


# ---------------------------------------------------------------------------
# Trade aggregation — nwt_trade_outcomes is one row per LEG, not per TRADE.
# Multi-leg spreads (bull_call_spread, bear_put_spread, iron_condor) write
# one outcome row per leg (execution/engine.py::resolve_spread_legs), tied
# together via nwt_portfolio_ledger.spread_group_id. Any caller that treats
# a raw outcome row as one trade over-counts: a single losing 4-leg iron
# condor looks like 4 losing trades, and a spread-heavy strategy clears a
# trade-count gate (mutation "30 to observe", learning-gate style checks) on
# far fewer real trades than the gate intends. Every caller needing "how
# many trades" or "this trade's combined PnL" must go through here — never
# query nwt_trade_outcomes directly for those.
# ---------------------------------------------------------------------------

def get_distinct_trade_pnls(
    conn,
    strategy_id: str = None,
    strategy_prefix: str = None,
    archetype: str = None,
    closed_after: datetime = None,
    order: str = "ASC",
    limit: int = None,
) -> list:
    """
    One row per real trade. A trade's identity is
    COALESCE(spread_group_id, position_id, id): single-leg trades key on
    position_id; multi-leg spreads key on the shared spread_group_id; the
    final id fallback covers legacy rows written before position_id existed
    (same fallback fetch_unprocessed_closed_positions already relies on —
    each such row was already being treated as its own trade, so this is not
    a behavior change for them).

    pnl is COALESCE(pnl_adjusted, pnl) summed across the trade's legs — None
    only when every leg in the trade has no pnl recorded, matching the prior
    per-row "exclude if no pnl data" behavior instead of silently reporting
    a fabricated $0 trade.

    Filters — pass at most one of strategy_id (exact) / strategy_prefix
    (LIKE '<prefix>%', e.g. 'C' for every Track C strategy) / archetype
    (exact). closed_after further restricts to trades whose last leg closed
    on/after that timestamp (e.g. "trades closed today").

    Returns [(pnl_or_None, closed_at), ...] ordered by closed_at.
    """
    if sum(x is not None for x in (strategy_id, strategy_prefix, archetype)) > 1:
        raise ValueError("pass at most one of strategy_id / strategy_prefix / archetype")
    if order not in ("ASC", "DESC"):
        raise ValueError("order must be ASC or DESC")

    clauses = []
    params: list = []
    if strategy_id is not None:
        clauses.append("to_.strategy_id = %s")
        params.append(strategy_id)
    elif strategy_prefix is not None:
        clauses.append("to_.strategy_id LIKE %s")
        params.append(f"{strategy_prefix}%")
    elif archetype is not None:
        clauses.append("to_.archetype = %s")
        params.append(archetype)
    if closed_after is not None:
        clauses.append("to_.closed_at >= %s")
        params.append(closed_after)
    filter_sql = ("AND " + " AND ".join(clauses)) if clauses else ""

    limit_sql = "LIMIT %s" if limit is not None else ""
    if limit is not None:
        params.append(limit)

    query = f"""
        SELECT SUM(COALESCE(to_.pnl_adjusted, to_.pnl)) AS pnl,
               MAX(to_.closed_at) AS closed_at
        FROM nwt_trade_outcomes to_
        LEFT JOIN nwt_portfolio_ledger pl ON pl.position_id = to_.position_id
        WHERE to_.closed_at IS NOT NULL
          {filter_sql}
        GROUP BY COALESCE(pl.spread_group_id, to_.position_id, to_.id)
        ORDER BY closed_at {order}
        {limit_sql}
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [(float(r[0]) if r[0] is not None else None, r[1]) for r in rows]


def count_distinct_trades(conn, strategy_id: str = None, strategy_prefix: str = None) -> int:
    """Count of real trades (multi-leg spreads collapsed to one), for trade-count gates/thresholds."""
    return len(get_distinct_trade_pnls(conn, strategy_id=strategy_id, strategy_prefix=strategy_prefix))


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
    sizing_multiplier: float = None,
) -> None:
    """
    INSERT into nwt_ticket_decisions. NEVER UPDATE nwt_tickets.

    sizing_multiplier: optional 0-1 factor (RISK_AGENT Rules 3/7 — slippage
    expansion, regime confidence < 0.4). execution_agent.py reads this back
    and multiplies sized_notional by it before submitting the order. This is
    the actual sizing-reduction mechanism; Rules 3/7 logging a WARNING with
    no corresponding effect was the original bug.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_decisions (ticket_id, decision, reasoning, decided_by, sizing_multiplier)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (ticket_id, decision, reasoning, decided_by, sizing_multiplier),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Ticket claims — atomic single-owner claim + lease, protects the window
# between "ticket selected" and "decision written" (see
# db/migrate_2026_07_execution_safety.sql for the schema and why this
# exists: crontab.txt has no overlap guard on the 5-minute agents, and their
# ticket-selection queries only mark a ticket handled AFTER slow external
# Alpaca calls complete — a real TOCTOU window for duplicate order placement).
# ---------------------------------------------------------------------------

TICKET_CLAIM_LEASE_SECONDS = 300  # see execution/engine.py's POLL_MAX/POLL_INTERVAL note below
# NOTE: 300s is a starting margin, not a substitute for renewal. engine.py's
# poll_order_until_filled alone has a worst case of
# POLL_MAX(10) x (POLL_INTERVAL(3s) + alpaca_get's 15s timeout) = 180s, and
# execution_agent.py's spread-leg resolution (chain fetches + per-leg price
# fetches) can run ~100-115s — close enough to a fixed lease that a
# legitimate slow run could still have its lease expire mid-processing.
# renew_ticket_claim() below is the real fix: call it right before entering
# a known-slow stretch so that stretch gets a fresh full lease budget
# regardless of how much of the original lease was already spent.


def claim_ticket(conn, ticket_id: str, worker_id: str,
                  lease_seconds: int = TICKET_CLAIM_LEASE_SECONDS) -> bool:
    """
    Atomically claim a ticket for exclusive processing.

    Returns True  -> this call now owns the claim; caller may process the
                      ticket and must call release_ticket_claim() when done.
    Returns False -> the ticket is not claimable right now: another live
                      (non-expired, in_progress) claim already holds it, or
                      it was already completed (status='done' is permanent —
                      a finished ticket can never be reclaimed, even after
                      its lease would otherwise look expired).

    Race-safe under concurrent callers: Postgres takes a row lock on the
    conflicting unique key during INSERT ... ON CONFLICT, so concurrent
    claim_ticket() calls for the same ticket_id serialize. The second caller's
    UPDATE branch is evaluated against the first caller's already-committed
    row, and the WHERE guard below only allows the update through if that row
    is 'failed' (retryable) or an expired 'in_progress' lease — so at most
    one caller's RETURNING produces a row, and 'done' is a true dead end.

    A worker that crashes mid-processing leaves status='in_progress' with a
    lease that eventually expires; the WHERE guard's
    "lease_expires_at < NOW()" branch lets a later run reclaim it rather than
    the ticket being stuck forever.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_claims (ticket_id, claimed_by, lease_expires_at, status, updated_at)
            VALUES (%s, %s, NOW() + (%s * INTERVAL '1 second'), 'in_progress', NOW())
            ON CONFLICT (ticket_id) DO UPDATE
              SET claimed_by = EXCLUDED.claimed_by,
                  claimed_at = NOW(),
                  lease_expires_at = EXCLUDED.lease_expires_at,
                  status = 'in_progress',
                  updated_at = NOW()
              WHERE nwt_ticket_claims.status = 'failed'
                 OR (nwt_ticket_claims.status = 'in_progress'
                     AND nwt_ticket_claims.lease_expires_at < NOW())
            RETURNING ticket_id
            """,
            (ticket_id, worker_id, lease_seconds),
        )
        got_it = cur.fetchone() is not None
    conn.commit()
    return got_it


def release_ticket_claim(conn, ticket_id: str, status: str = "done") -> None:
    """Release a claim after processing completes (status='done') or a
    handled failure (status='failed') so the row stops holding the lease."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_ticket_claims SET status = %s, updated_at = NOW() WHERE ticket_id = %s",
            (status, ticket_id),
        )
    conn.commit()


def renew_ticket_claim(conn, ticket_id: str, worker_id: str,
                        lease_seconds: int = TICKET_CLAIM_LEASE_SECONDS) -> bool:
    """
    Extend the lease on a claim this worker still holds — call this right
    before a known-slow stretch of work (an external poll loop, multiple
    sequential API calls) so that stretch gets a fresh full lease budget
    instead of relying on the original claim-time lease being large enough
    to cover it. Only extends a claim this exact worker_id still owns with
    status='in_progress' — a worker that already lost its claim (lease
    already expired and reclaimed by someone else) gets False back and must
    not continue processing, since it is no longer the exclusive owner.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_ticket_claims
            SET lease_expires_at = NOW() + (%s * INTERVAL '1 second'), updated_at = NOW()
            WHERE ticket_id = %s AND claimed_by = %s AND status = 'in_progress'
            """,
            (lease_seconds, ticket_id, worker_id),
        )
        renewed = cur.rowcount > 0
    conn.commit()
    return renewed


# ---------------------------------------------------------------------------
# Force-close lifecycle — explicit terminal states, bounded exponential
# backoff. Replaces the old cooldown-only retry in risk_agent.py's Rule 12,
# which had no way to ever stop retrying a position that can never close
# (e.g. an expired option) or one that has failed indefinitely.
# ---------------------------------------------------------------------------

# 1m, 5m, 15m, 1h, 6h, 24h — matches db/migrate_2026_07_execution_safety.sql's
# design note. After this many failed attempts, escalate to
# FAILED_REQUIRES_HUMAN and stop generating new FORCE_CLOSE tickets.
FORCE_CLOSE_BACKOFF_MINUTES = [1, 5, 15, 60, 360, 1440]
FORCE_CLOSE_MAX_ATTEMPTS = len(FORCE_CLOSE_BACKOFF_MINUTES)
FORCE_CLOSE_ATTEMPTING_COOLDOWN_MINUTES = 15  # room for an in-flight attempt to resolve


def get_force_close_state(conn, position_id: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_force_close_state WHERE position_id = %s", (position_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def has_pending_force_close_ticket(conn, position_id: str) -> bool:
    """
    True if a FORCE_CLOSE ticket for this position already exists in
    nwt_tickets with no EXECUTION_ENGINE decision yet — i.e. it is still
    waiting to be claimed/processed by execution/engine.py.

    This is checked ahead of (and independent from) the time-based
    ATTEMPTING cooldown in schedule_force_close_attempt below. The cooldown
    alone is a crash-recovery heuristic — after
    FORCE_CLOSE_ATTEMPTING_COOLDOWN_MINUTES it assumes the prior attempt is
    abandoned and allows a retry. But "abandoned" and "still queued, just
    slow to get picked up" look identical from a pure clock read: if
    engine.py falls behind or is briefly down for longer than the cooldown,
    the first ticket is still perfectly valid and outstanding, yet the
    cooldown alone would let a second ticket be created for the same
    position. claim_ticket() only guarantees one WORKER per ticket_id — it
    does nothing to stop two DIFFERENT tickets for the same position from
    both existing and both eventually being processed. Checking for an
    actual outstanding ticket first closes that gap at the source, instead
    of relying only on close_position()'s idempotent WHERE status='open'
    guard to contain the consequences after the fact.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM nwt_tickets t
              WHERE t.type = 'FORCE_CLOSE'
                AND t.payload->>'position_id' = %s
                AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
                )
            )
            """,
            (position_id,),
        )
        (exists,) = cur.fetchone()
    return bool(exists)


def schedule_force_close_attempt(conn, position_id: str, asset: str) -> bool:
    """
    Single atomic decision: should a new FORCE_CLOSE ticket be created for
    this position right now? If yes, this call also records the attempt
    (state=ATTEMPTING, attempt_count+1, last_attempt_at=NOW()) in the same
    statement — there is no separate read-then-write step, so two
    overlapping risk_agent.py runs (crontab has no flock) cannot both decide
    to retry the same position in the same window.

    Returns True  -> caller should create a FORCE_CLOSE ticket now.
    Returns False -> caller must NOT create a ticket: a FORCE_CLOSE ticket
                      for this position is already outstanding
                      (has_pending_force_close_ticket), the position is
                      already terminal (SUCCESS/FAILED_TERMINAL/
                      FAILED_REQUIRES_HUMAN), still inside its backoff
                      cooldown, or a prior attempt may still be in flight.
                      If this call is what pushes attempt_count past
                      FORCE_CLOSE_MAX_ATTEMPTS, it also flips the state to
                      FAILED_REQUIRES_HUMAN and logs CRITICAL once — the
                      caller still gets False and must not create a ticket.
    """
    if has_pending_force_close_ticket(conn, position_id):
        return False

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO nwt_force_close_state
              (position_id, asset, state, attempt_count, last_attempt_at, updated_at)
            VALUES (%(position_id)s, %(asset)s, 'ATTEMPTING', 1, NOW(), NOW())
            ON CONFLICT (position_id) DO UPDATE SET
              state = CASE
                WHEN nwt_force_close_state.attempt_count + 1 > %(max_attempts)s
                  THEN 'FAILED_REQUIRES_HUMAN'
                ELSE 'ATTEMPTING'
              END,
              attempt_count = nwt_force_close_state.attempt_count + 1,
              last_attempt_at = NOW(),
              escalated_at = CASE
                WHEN nwt_force_close_state.attempt_count + 1 > %(max_attempts)s THEN NOW()
                ELSE nwt_force_close_state.escalated_at
              END,
              updated_at = NOW()
            WHERE
              nwt_force_close_state.state NOT IN ('SUCCESS', 'FAILED_TERMINAL', 'FAILED_REQUIRES_HUMAN')
              AND (
                nwt_force_close_state.state != 'FAILED_RETRYABLE'
                OR nwt_force_close_state.next_retry_at IS NULL
                OR nwt_force_close_state.next_retry_at <= NOW()
              )
              AND (
                nwt_force_close_state.state != 'ATTEMPTING'
                OR nwt_force_close_state.last_attempt_at IS NULL
                OR nwt_force_close_state.last_attempt_at
                   <= NOW() - (%(attempting_cooldown)s * INTERVAL '1 minute')
              )
            RETURNING state, attempt_count
            """,
            {
                "position_id": position_id,
                "asset": asset,
                "max_attempts": FORCE_CLOSE_MAX_ATTEMPTS,
                "attempting_cooldown": FORCE_CLOSE_ATTEMPTING_COOLDOWN_MINUTES,
            },
        )
        row = cur.fetchone()
    conn.commit()

    if row is None:
        return False  # terminal, in cooldown, or a prior attempt still in flight

    if row["state"] == "FAILED_REQUIRES_HUMAN":
        log_system_event(
            conn, "CRITICAL", "risk_agent",
            f"FORCE_CLOSE exhausted {FORCE_CLOSE_MAX_ATTEMPTS} attempts for {asset} "
            f"(position_id={position_id}) — escalated to FAILED_REQUIRES_HUMAN, "
            f"no further automatic retries. Needs manual intervention.",
            {"position_id": position_id, "asset": asset, "attempt_count": row["attempt_count"]},
        )
        return False

    return True  # state == 'ATTEMPTING' — caller should create the ticket
