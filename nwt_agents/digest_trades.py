"""Operational closed-trade totals, without manufacturing learning outcomes."""
from datetime import datetime, timedelta, timezone

from learning_agent import compute_pnl_adjusted


def fetch_digest_trades(conn, day):
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    # One database snapshot prevents a concurrently inserted learning outcome
    # from being counted alongside (or instead of neither) its ledger fallback.
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH outcomes AS (
                SELECT SUM(COALESCE(o.pnl_adjusted, o.pnl)) AS pnl,
                       MAX(o.closed_at) AS closed_at
                FROM nwt_trade_outcomes o
                LEFT JOIN nwt_portfolio_ledger l ON l.position_id = o.position_id
                WHERE o.closed_at IS NOT NULL
                  AND o.strategy_id IS DISTINCT FROM 'QA_PAPER_LIFECYCLE'
                GROUP BY COALESCE(l.spread_group_id, o.position_id, o.id)
            )
            SELECT pnl, closed_at, NULL::jsonb AS recovered_position
            FROM outcomes WHERE closed_at >= %s AND closed_at < %s
            UNION ALL
            SELECT NULL::numeric, l.exit_time, to_jsonb(l)
            FROM nwt_portfolio_ledger l
            JOIN nwt_position_attribution a ON a.position_id = l.position_id
            WHERE l.status = 'closed' AND l.lifecycle_state = 'CLOSED'
              AND l.asset_type = 'equity' AND l.spread_group_id IS NULL
              AND l.strategy_id IS DISTINCT FROM 'QA_PAPER_LIFECYCLE'
              AND a.attribution_status = 'verified_quantity_and_cost'
              AND l.qty > 0 AND l.entry_price > 0 AND l.exit_price > 0
              AND l.direction IN ('long', 'short')
              AND l.exit_time >= %s AND l.exit_time < %s
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_trade_outcomes o
                  WHERE o.position_id = l.position_id
                     OR (o.position_id IS NULL AND o.symbol = l.asset
                         AND o.entry_time = l.entry_time AND o.direction = l.direction)
              )
            """, (start, end, start, end),
        )
        rows = cur.fetchall()
    trades = []
    for pnl, closed_at, position in rows:
        if position is not None:
            entry = float(position['entry_price'])
            pnl, _, _, _ = compute_pnl_adjusted(
                entry, float(position['exit_price']), position['direction'],
                entry * float(position['qty']), position.get('entry_bid'),
                position.get('entry_ask'), position.get('exit_bid'),
                position.get('exit_ask'), 'equity',
            )
        trades.append((float(pnl) if pnl is not None else None, closed_at))
    return trades
