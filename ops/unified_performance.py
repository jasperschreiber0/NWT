"""Account-level truth and hypothesis-to-fill attribution, without double-counting costs."""
import json
import sqlite3
from datetime import datetime,timezone
from paper_io import Paper,STATE,atomic,stamp


def cash_adjustment(activities):
    amount=0.;ambiguous=[]
    for row in activities:
        kind=row.get('activity_type')
        if kind in ('CSD','CSW'):amount+=float(row['net_amount'])
        elif kind in ('ACATC','ACATS','JNLC','JNLS','TRANS'):ambiguous.append(row['id'])
    return dict(net_external_cash=amount,ambiguous_transfer_ids=ambiguous)


def expenses(config,elapsed_days):
    services=['alpaca','hetzner','railway','supabase']
    missing=[s for s in services if not isinstance(config.get(s),dict) or config[s].get('monthly_usd') is None]
    if missing:return dict(complete=False,missing=missing,accrued_usd=None)
    amounts=[float(config[s]['monthly_usd']) for s in services]
    import math
    if any(not math.isfinite(x) or x<0 for x in amounts):raise ValueError('Invalid monthly operating costs')
    monthly=sum(amounts)
    return dict(complete=True,monthly_usd=monthly,accrued_usd=monthly*elapsed_days/(365.25/12),
                monthly_break_even_on_5000=monthly/5000)


def trace(rows):
    result=[]
    for row in rows:
        p=row.get('payload') or {};meta=p.get('research') or {};q=meta.get('decision_quote') or {}
        entry=float(row['entry_price']) if row.get('entry_price') is not None else None
        ask=float(q['ap']) if q.get('ap') else None
        result.append(dict(position_id=str(row['position_id']),ticket_id=str(row.get('ticket_id') or ''),
            strategy=row.get('strategy_id'),hypothesis=meta.get('experiment'),hypothesis_details=meta.get('hypothesis'),
            decisions=row.get('decisions') or [],broker_order_id=row.get('alpaca_order_id'),status=row['status'],
            asset=row['asset'],qty=row.get('qty'),entry_time=row.get('entry_time'),exit_time=row.get('exit_time'),
            entry_price=entry,exit_price=row.get('exit_price'),gross_realized=row.get('pnl'),modeled_adjusted_realized=row.get('pnl_adjusted'),
            entry_slippage_vs_decision_ask=entry/ask-1 if entry and ask else None,
            limitation=None if meta else 'Legacy position lacks a registered discovery hypothesis'))
    return result


def fetch_activities(paper,after):
    rows=[];token=None;seen=set()
    for _ in range(100):
        params=dict(after=after,direction='asc',page_size=100)
        if token:params['page_token']=token
        page=paper.get('/v2/account/activities',params)
        if not isinstance(page,list):raise ValueError('Invalid account activity response')
        rows.extend(page)
        if len(page)<100:return rows
        token=page[-1]['id']
        if token in seen:raise ValueError('Repeated account activity page')
        seen.add(token)
    raise ValueError('Incomplete account activity history')


def main():
    paper=Paper();now=datetime.now(timezone.utc);folder=STATE/'performance';folder.mkdir(exist_ok=True)
    account=paper.get('/v2/account');positions=paper.get('/v2/positions')
    db=sqlite3.connect(folder/'snapshots.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS snapshots(observed TEXT PRIMARY KEY,payload TEXT)')
    first=db.execute('SELECT observed,payload FROM snapshots ORDER BY observed LIMIT 1').fetchone()
    baseline=json.loads(first[1]) if first else dict(equity=float(account['equity']),observed_at=now.isoformat())
    try:
        activities=fetch_activities(paper,baseline['observed_at'])
        flows=cash_adjustment(activities);activity_status='COMPLETE'
    except (RuntimeError,ValueError,KeyError):
        activities=[];flows=dict(net_external_cash=None,ambiguous_transfer_ids=[]);activity_status='UNAVAILABLE'
    elapsed=max(0,(now-stamp(baseline['observed_at'])).total_seconds()/86400)
    config_path=STATE/'operating-costs.json'
    costs=expenses(json.loads(config_path.read_text()) if config_path.exists() else {},elapsed)
    change=float(account['equity'])-float(baseline['equity'])
    profit=change-flows['net_external_cash'] if activity_status=='COMPLETE' and not flows['ambiguous_transfer_ids'] else None
    conn=paper.db()
    conn.set_session(readonly=True)
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT l.*,o.pnl,o.pnl_adjusted,t.payload,COALESCE((SELECT jsonb_agg(jsonb_build_object('decision',d.decision,'reason',d.reasoning,'at',d.created_at) ORDER BY d.created_at) "
            "FROM nwt_ticket_decisions d WHERE d.ticket_id=l.ticket_id),'[]'::jsonb) decisions FROM nwt_portfolio_ledger l "
            "LEFT JOIN nwt_trade_outcomes o ON o.position_id=l.position_id LEFT JOIN nwt_tickets t ON t.ticket_id=l.ticket_id "
            "WHERE l.entry_time >= '2026-09-15' ORDER BY l.entry_time")
        rows=[dict(x) for x in q.fetchall()]
        q.execute("SELECT t.ticket_id,t.created_at,t.type,t.payload,COALESCE((SELECT jsonb_agg(jsonb_build_object('decision',d.decision,'reason',d.reasoning)) "
            "FROM nwt_ticket_decisions d WHERE d.ticket_id=t.ticket_id),'[]'::jsonb) decisions FROM nwt_tickets t "
            "WHERE t.from_agent='NWT_RESEARCH_BRIDGE' ORDER BY t.created_at")
        proposals=[dict(x) for x in q.fetchall()]
    conn.close()
    from profit_attribution import aggregate
    grouped=aggregate(rows)
    report=dict(observed_at=now.isoformat(),status='OK',equity=float(account['equity']),cash=float(account['cash']),
        unrealized_broker=sum(float(p['unrealized_pl']) for p in positions),
        reference_100k_change=float(account['equity'])-100000,reference_note='Reference difference only; not verified lifetime cash-flow-adjusted profit',
        measured_since=baseline['observed_at'],measured_equity_change=change,flows=flows,activity_status=activity_status,
        measured_trading_profit=profit,operating_costs=costs,
        measured_after_operating_costs=profit-costs['accrued_usd'] if profit is not None and costs['complete'] else None,
        broker_reported_fee_activities=sum(float(x.get('net_amount') or 0) for x in activities if x.get('activity_type')=='FEE'),
        fee_note='Broker fees already affect account equity; never deducted twice. Trade adjusted outcomes use separate modeled costs.',
        completed_trade_groups=grouped,trace=trace(rows),proposals=proposals,
        limitations=['Transfer journals require classification before net profit is claimed','Legacy missing links remain explicit'])
    db.execute('INSERT INTO snapshots VALUES (?,?)',(now.isoformat(),json.dumps(report,default=str)));db.commit();db.close()
    atomic(STATE/'unified-performance.json',report)
    print(json.dumps({k:report[k] for k in ['status','equity','unrealized_broker','measured_trading_profit','operating_costs']}))


if __name__=='__main__':main()
