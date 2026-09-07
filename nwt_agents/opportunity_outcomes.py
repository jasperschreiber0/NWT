"""Canonical opportunity outcome lanes and read-only edge scoreboard queries."""

import json
import uuid
from typing import Any


LANES = ("RAW_SHADOW", "BRAIN_SHADOW", "RISK_SHADOW", "PAPER", "LIVE")
OPPORTUNITY_NAMESPACE = uuid.UUID("7c5a3b7e-6a20-4ee0-9f0c-2e1d7d0d4a11")


def opportunity_id_for(strategy_id: str, symbol: str, run_date: Any, track: str,
                       genome_version=None, poll_slot="") -> str:
    """Stable identity for one strategy opportunity on one evaluation date."""
    key = json.dumps([strategy_id, symbol or "", str(run_date), track,
                      genome_version or 0, poll_slot], separators=(",", ":"))
    return str(uuid.uuid5(OPPORTUNITY_NAMESPACE, key))


def upsert_outcome(conn, opportunity_id: str, lane: str, data: dict[str, Any]) -> None:
    """Create or update one lane without changing trading decisions."""
    if lane not in LANES:
        raise ValueError(f"invalid outcome lane: {lane}")
    required = ("strategy_id", "symbol")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    fields = {
        "strategy_id": data["strategy_id"],
        "symbol": data["symbol"],
        "direction": data.get("direction"),
        "track": data.get("track"),
        "regime": json.dumps(data["regime"]) if data.get("regime") is not None else None,
        "proposed_qty": data.get("proposed_qty"),
        "applied_qty": data.get("applied_qty"),
        "entry_price": data.get("entry_price"),
        "exit_price": data.get("exit_price"),
        "pnl": data.get("pnl"),
        "pnl_pct": data.get("pnl_pct"),
        "cost": data.get("cost"),
        "outcome": data.get("outcome"),
        "decision": data.get("decision"),
        "decision_reason": data.get("decision_reason"),
        "source_ticket_id": data.get("source_ticket_id"),
        "source_decision_id": data.get("source_decision_id"),
        "opened_at": data.get("opened_at"),
        "closed_at": data.get("closed_at"),
    }
    columns = ["opportunity_id", "lane", *fields]
    values = [opportunity_id, lane, *fields.values()]
    placeholders = ", ".join(["%s"] * len(values))
    # Sparse retry payloads must not erase completed outcomes or attribution.
    updates = ", ".join(
        f"{column}=COALESCE(EXCLUDED.{column}, nwt_opportunity_outcomes.{column})"
        for column in fields
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO nwt_opportunity_outcomes ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (opportunity_id, lane) DO UPDATE SET
              {updates}, updated_at=NOW()
            """,
            values,
        )
    conn.commit()


SCOREBOARD_QUERY = """
SELECT strategy_id,
       lane,
       COUNT(*) AS opportunities,
       COUNT(*) FILTER (WHERE closed_at IS NOT NULL) AS completed,
       ROUND(AVG(pnl_pct)::numeric, 6) AS expectancy_pct,
       ROUND(AVG(cost)::numeric, 6) AS avg_cost,
       COUNT(*) FILTER (WHERE decision IS NULL OR outcome IS NULL) AS incomplete
FROM nwt_opportunity_outcomes
GROUP BY strategy_id, lane
ORDER BY strategy_id, lane
"""


def fetch_scoreboard(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(SCOREBOARD_QUERY)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

