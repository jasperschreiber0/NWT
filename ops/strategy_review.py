"""Frozen paper experiment budgets and descriptive loss diagnosis; no promotion authority."""
import math
from collections import defaultdict
from datetime import datetime, timezone
from profit_attribution import aggregate
from psycopg2.extras import RealDictCursor

POLICY = dict(version='paper-budget-20261004-v1', window_start='2026-09-15T00:00:00+00:00',
    new_cohort_start='2026-10-04T00:00:00+00:00', rolling_groups=20,
    restricted_minimum_groups=5, restricted_profit_factor=.8,
    retirement_minimum_groups=20, entry_notional_cap=500., strategy_open_cap=1000.,
    new_cohort_loss_budget=100., material_accounting_difference=1.,
    promotion='REVIEW_ONLY; existing validation and reliability gates remain required')
RANK={'UNCHANGED_UNPROVEN':0,'LIMITED_EXPERIMENT':1,'SHADOW_ONLY':2,'ACCOUNTING_HOLD':3}


def review(rows):
    attribution=aggregate(rows); by_strategy=defaultdict(list); diagnoses=[]
    for trade in attribution['complete_groups']:
        flags=[]
        discrepancy=abs(trade['ledger_price_pnl']-trade['gross'])
        if discrepancy>POLICY['material_accounting_difference']: flags.append('ACCOUNTING_REVIEW_REQUIRED')
        elif discrepancy>.01: flags.append('MINOR_ACCOUNTING_DIFFERENCE; RETAIN_FOR_REVIEW')
        if 'reconcil' in trade['exit_reason'] or 'incident' in trade['exit_reason']: flags.append('RECOVERY_RECORD_NOT_CLEAN_STRATEGY_EVIDENCE')
        if trade['gross']>=0 and trade['net']<0: flags.append('MODELED_COSTS_TURNED_GAIN_INTO_LOSS')
        if trade['gross']<0: flags.append('PRICE_LOSS_BEFORE_MODELED_COSTS')
        if 'stop' in trade['exit_reason']: flags.append('STOP_EXIT; DOES_NOT_PROVE_STOP_WAS_WRONG')
        if 'delayed' in trade['exit_reason']: flags.append('DELAYED_EXIT; CAUSAL_PRICE_EFFECT_NOT_MEASURED')
        diagnosis=dict(trade,flags=flags,signal_vs_timing='UNRESOLVED_WITHOUT_MATCHED_COUNTERFACTUAL',
            execution_slippage='UNRESOLVED_WITHOUT_LINKED_DECISION_QUOTE')
        diagnoses.append(diagnosis)
        by_strategy[trade['strategy']].append(diagnosis)
    strategies=[]
    for strategy,trades in sorted(by_strategy.items()):
        ordered=sorted(trades,key=lambda t:t['exit_time'])[-POLICY['rolling_groups']:]
        net=sum(t['net'] for t in ordered)
        gains=sum(max(t['net'],0) for t in ordered);losses=-sum(min(t['net'],0) for t in ordered)
        pf=gains/losses if losses else None
        bad=any('ACCOUNTING_REVIEW_REQUIRED' in t['flags'] or 'RECOVERY_RECORD_NOT_CLEAN_STRATEGY_EVIDENCE' in t['flags'] for t in ordered)
        cohort=[t for t in trades if datetime.fromisoformat(t['entry_time'])>=datetime.fromisoformat(POLICY['new_cohort_start'])]
        cohort_net=sum(t['net'] for t in cohort)
        state='UNCHANGED_UNPROVEN';reason='Insufficient evidence for an allocation change'
        if bad:
            state='ACCOUNTING_HOLD';reason='Recent outcome records require accounting review'
        elif len(ordered)>=POLICY['restricted_minimum_groups'] and net<0 and pf is not None and pf<POLICY['restricted_profit_factor']:
            state='LIMITED_EXPERIMENT';reason='Negative recent adjusted results and profit factor below 0.8'
            if len(ordered)>=POLICY['retirement_minimum_groups']:
                state='SHADOW_ONLY';reason='Twenty recent completed groups failed the fixed profitability rule'
        if cohort_net<=-POLICY['new_cohort_loss_budget']:
            state='SHADOW_ONLY';reason='New-cohort adjusted loss budget exhausted'
        strategies.append(dict(strategy=strategy,state=state,reason=reason,completed_groups=len(ordered),
            adjusted_net=net,profit_factor=pf,new_cohort_net=cohort_net,
            entry_cap=POLICY['entry_notional_cap'] if state=='LIMITED_EXPERIMENT' else None,
            open_cap=POLICY['strategy_open_cap'] if state=='LIMITED_EXPERIMENT' else None))
    return dict(policy=POLICY,strategies=strategies,diagnoses=diagnoses,excluded_groups=attribution['excluded_groups'],
        operating_costs=dict(target_capital=5000,monthly_total=None,monthly_break_even_return=None,
            status='INCOMPLETE: confirm billing interval for $111 and Railway/Supabase charges; not deducted from results'),
        note='Adjusted losses include modeled costs. Thresholds are experiment controls, not statistical proof. Existing exits are unaffected.')


def latch(conn, decisions):
    """Restrictions can tighten automatically; never silently restore allocation."""
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        for decision in sorted(decisions,key=lambda d:d['strategy']):
            q.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",('strategy-budget:'+decision['strategy'],))
            q.execute("SELECT payload FROM nwt_system_log WHERE component='strategy_budget' AND payload->>'strategy'=%s ORDER BY created_at DESC,id DESC LIMIT 1",(decision['strategy'],))
            row=q.fetchone(); old=row['payload'] if row else None
            if old and RANK[old['state']]>=RANK[decision['state']]:
                decision.update(state=old['state'],reason=old['reason'],restriction_latched=True)
                continue
            if decision['state']!='UNCHANGED_UNPROVEN':
                import json
                q.execute("INSERT INTO nwt_system_log(level,component,message,payload) VALUES ('WARNING','strategy_budget',%s,%s::jsonb)",
                    (decision['state'],json.dumps(dict(decision,policy_version=POLICY['version']))))
    conn.commit()
    return decisions


def load_rows(conn, strategy=None):
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT l.*,o.pnl,o.pnl_adjusted FROM nwt_portfolio_ledger l LEFT JOIN nwt_trade_outcomes o ON o.position_id=l.position_id "
            "WHERE l.entry_time >= %s AND COALESCE(l.strategy_id,'') <> 'QA_PAPER_LIFECYCLE' " +
            ('AND l.strategy_id=%s' if strategy else ''),
            (POLICY['window_start'],strategy) if strategy else (POLICY['window_start'],))
        return [dict(r) for r in q.fetchall()]


def entry_gate(conn,payload,broker_open_orders):
    strategy=payload.get('strategy_id')
    if not isinstance(strategy,str) or not strategy.strip():return 'Missing strategy identity'
    if strategy=='QA_PAPER_LIFECYCLE':return None
    result=review(load_rows(conn,strategy))
    decisions=result['strategies'] or [dict(strategy=strategy,state='UNCHANGED_UNPROVEN',reason='No completed groups')]
    decision=next((s for s in latch(conn,decisions) if s['strategy']==strategy),None)
    if not decision or decision['state']=='UNCHANGED_UNPROVEN':return None
    if decision['state'] in ('ACCOUNTING_HOLD','SHADOW_ONLY'):
        return decision['state']+': '+decision['reason']
    if payload.get('asset_type')!='equity' or payload.get('direction')!='long':
        return 'LIMITED_EXPERIMENT: only bounded long-equity entries supported; options/short sizing needs separate validation'
    notional=float(payload.get('sized_notional',0))
    if not math.isfinite(notional) or not 0<notional<=POLICY['entry_notional_cap']:
        return 'LIMITED_EXPERIMENT: entry budget exceeds $500'
    if broker_open_orders():return 'LIMITED_EXPERIMENT: pending broker order; wait for reconciliation'
    with conn.cursor() as q:
        q.execute("SELECT qty,entry_price,asset_type FROM nwt_portfolio_ledger WHERE strategy_id=%s AND status IN ('open','suspect')",(strategy,))
        exposure=sum(abs(float(qty)*float(price))*(100 if kind=='option' else 1) for qty,price,kind in q.fetchall())
    if not math.isfinite(exposure) or exposure+notional>POLICY['strategy_open_cap']:
        return 'LIMITED_EXPERIMENT: strategy entry-cost exposure would exceed $1000'
    payload['_experiment_budget']=notional
    return None
