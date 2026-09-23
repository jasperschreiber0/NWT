"""Separate complete trade results, accounting gaps and execution measurements."""
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from psycopg2.extras import RealDictCursor


def aggregate(rows):
    groups=defaultdict(list)
    for row in rows:groups[str(row.get('spread_group_id') or row['position_id'])].append(row)
    by_strategy=defaultdict(list);excluded=[]
    for key,legs in groups.items():
        if len({str(x['position_id']) for x in legs})!=len(legs):
            excluded.append({'group':key,'reason':'duplicate outcome attribution'});continue
        if any(x['status']!='closed' or x.get('pnl_adjusted') is None for x in legs):
            excluded.append({'group':key,'reason':'open or missing completed outcome'});continue
        strategies={x.get('strategy_id') or 'UNATTRIBUTED' for x in legs}
        if len(strategies)!=1:
            excluded.append({'group':key,'reason':'conflicting strategy attribution'});continue
        strategy=next(iter(strategies))
        entry=min(x['entry_time'] for x in legs);end=max(x['exit_time'] for x in legs)
        gross=sum(float(x['pnl']) for x in legs);net=sum(float(x['pnl_adjusted']) for x in legs)
        by_strategy[strategy].append({'group':key,'gross':gross,'net':net,'cost_adjustment':gross-net,
            'hours_held':(end-entry).total_seconds()/3600,'exit_reason':','.join(sorted(set(x.get('exit_reason') or 'unknown' for x in legs))),
            'entry_hour_et':entry.astimezone(ZoneInfo('America/New_York')).hour,
            'long_buy_hold_same_interval_gross':sum((float(x['exit_price'])-float(x['entry_price']))*float(x['qty'])*(100 if x['asset_type']=='option' else 1) for x in legs if x['asset_type']=='equity'),
            'equity_only':all(x['asset_type']=='equity' for x in legs)})
    results=[]
    for strategy,trades in by_strategy.items():
        wins=sum(x['net']>0 for x in trades);positive=sum(max(x['net'],0) for x in trades);negative=-sum(min(x['net'],0) for x in trades)
        exits=defaultdict(lambda:{'trades':0,'net':0});hours=defaultdict(lambda:{'trades':0,'net':0})
        for t in trades:
            for bucket,key in [(exits,t['exit_reason']),(hours,str(t['entry_hour_et']))]:bucket[key]['trades']+=1;bucket[key]['net']+=t['net']
        results.append({'strategy':strategy,'complete_trades':len(trades),'net':sum(x['net'] for x in trades),
            'gross':sum(x['gross'] for x in trades),'cost_adjustment':sum(x['cost_adjustment'] for x in trades),
            'win_rate':wins/len(trades),'profit_factor':positive/negative if negative else None,
            'average_net':statistics.mean(x['net'] for x in trades),'median_hours_held':statistics.median(x['hours_held'] for x in trades),
            'by_exit_reason':dict(exits),'by_entry_hour_et':dict(hours),
            'equity_buy_hold_same_intervals_gross':sum(x['long_buy_hold_same_interval_gross'] for x in trades if x['equity_only'])})
    return {'strategies':sorted(results,key=lambda x:x['net']),'excluded_groups':excluded,
            'benchmark_note':'Same-asset long buy-and-hold over each executed equity holding interval only; not a passive whole-period portfolio comparison.',
            'timing_note':'Descriptive buckets, not causal proof that changing an entry hour or exit rule improves returns.'}


def build(conn, get=None):
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        # Include every leg of groups opened during this paper research window.
        q.execute("SELECT l.*,o.pnl,o.pnl_adjusted FROM nwt_portfolio_ledger l LEFT JOIN nwt_trade_outcomes o ON o.position_id=l.position_id "
                  "WHERE l.entry_time >= '2026-09-15T00:00:00Z' AND COALESCE(l.strategy_id,'') <> 'QA_PAPER_LIFECYCLE'")
        rows=[dict(x) for x in q.fetchall()]
    data=aggregate(rows)
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
    from small_account import assess
    groups=defaultdict(list)
    for row in rows:groups[str(row.get('spread_group_id') or row['position_id'])].append(row)
    data['small_account_feasibility']=[{'group':key,**assess(legs)} for key,legs in groups.items()]
    if get is not None:
        recent=sorted(rows,key=lambda x:x['entry_time'],reverse=True)[:50]
        data['execution_measurements']=execution_measurements(recent,get)
    data.update(observed_at=datetime.now(timezone.utc).isoformat(),window_start='2026-09-15',execution_enabled=False)
    return data


def execution_measurements(rows,get):
    """GET-only order receipts; missing measurements remain missing."""
    results=[]
    for oid in sorted({str(r['alpaca_order_id']) for r in rows if r.get('alpaca_order_id')}):
        order=get('/orders/'+oid)
        filled=order.get('filled_at');submitted=order.get('submitted_at')
        latency=(datetime.fromisoformat(filled.replace('Z','+00:00'))-datetime.fromisoformat(submitted.replace('Z','+00:00'))).total_seconds() if filled and submitted else None
        results.append({'order_id':oid,'symbol':order.get('symbol'),'status':order.get('status'),
                        'fill_delay_seconds':latency,'filled_qty':order.get('filled_qty'),
                        'fill_price':order.get('filled_avg_price'),
                        'decision_to_fill_slippage':None,'missing_reason':'No verified decision-time quote linkage'})
    return results
