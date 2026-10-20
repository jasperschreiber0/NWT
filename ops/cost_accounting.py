"""Offline accounting primitives. Decimal USD, explicit evidence, no billing calls.

Fill-price P&L already incorporates execution spread/slippage. Scenario haircuts
are separate stress estimates, never additional actual fees. Coverage declarations
must come from a reconciled source manifest, not absence of charge rows.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

REQUIRED_SERVICES = ('openai', 'alpaca', 'hetzner', 'railway', 'supabase')


def number(value):
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError('Non-finite amount')
    return x


def stamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(d, datetime) or d.tzinfo is None:
        raise ValueError('Timezone required')
    return d.astimezone(timezone.utc)


def unique(rows):
    """Replay idempotency. Conflicting revisions require reconciliation."""
    found: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r.get('source_id')
        if not key:
            raise ValueError('Missing source identity')
        if key in found and found[key] != r:
            raise ValueError('Conflicting source record: ' + key)
        found[key] = r
    return list(found.values())


def trade_costs(fills, *, fees_complete=False, fee_manifest=None, stress_usd=None,
                expected_instruments=None, fill_manifest=None):
    """One complete trade's incremental fills, including every spread leg.

    Each execution is identified by source_id, instrument, side, phase, qty,
    price, multiplier, currency, executed_at and fee_usd (null = unknown).
    Cumulative order snapshots are deliberately rejected; normalize from actual
    execution activities first. No arbitrary assignment of account fees to trades.
    """
    rows = unique(fills)
    legs = defaultdict(list)
    gaps = []
    for r in rows:
        if r.get('currency') != 'USD' or r.get('quantity_kind') != 'incremental':
            raise ValueError('USD incremental execution evidence required')
        if r.get('phase') not in ('entry', 'exit') or r.get('side') not in ('buy', 'sell'):
            raise ValueError('Invalid fill phase/side')
        stamp(r['executed_at'])
        if any(number(r[k]) <= 0 for k in ('qty', 'price', 'multiplier')):
            raise ValueError('Positive fill quantities/prices/multipliers required')
        legs[r['instrument']].append(r)
    gross = Decimal(0)
    if not fill_manifest or not expected_instruments or set(legs) != set(expected_instruments):
        gaps.append('unverified_fill_or_leg_coverage')
    for instrument, leg in legs.items():
        entry = [r for r in leg if r['phase'] == 'entry']
        exits = [r for r in leg if r['phase'] == 'exit']
        sides = {r['side'] for r in entry}
        if (len(sides) != 1 or not exits or
            any(r['side'] in sides for r in exits) or
            len({number(r['multiplier']) for r in leg}) != 1 or
            sum(number(r['qty']) for r in entry) != sum(number(r['qty']) for r in exits)):
            gaps.append('incomplete_or_conflicting_leg:' + instrument)
        if entry and exits and min(stamp(r['executed_at']) for r in exits) < min(stamp(r['executed_at']) for r in entry):
            gaps.append('exit_before_entry:' + instrument)
        gross += sum(number(r['qty']) * number(r['price']) * number(r['multiplier']) *
                     (1 if r['side'] == 'sell' else -1) for r in leg)
    if not rows:
        gaps.append('missing_fills')
    gross_value = None if gaps else gross
    if not fees_complete or not fee_manifest or any(r.get('fee_usd') is None for r in rows):
        gaps.append('unverified_execution_fee_coverage')
    known_fees = sum((number(r['fee_usd']) for r in rows if r.get('fee_usd') is not None), Decimal(0))
    if any(number(r['fee_usd']) < 0 for r in rows if r.get('fee_usd') is not None):
        raise ValueError('Negative fee requires separate rebate evidence')
    complete = not gaps
    return dict(currency='USD', execution_ids=sorted(r['source_id'] for r in rows),
                gross_fill_pnl=gross_value, known_fees_usd=known_fees,
                execution_net_usd=gross-known_fees if complete else None,
                execution_costs_complete=complete, missing=gaps, fee_manifest=fee_manifest,
                spread_slippage='embedded_in_fill_prices_not_deducted_again',
                stress_pnl_usd=gross-number(stress_usd) if gross_value is not None and stress_usd is not None else None,
                fully_loaded_net_usd=None,
                operating_cost_note='Account-level operating expenses need explicit allocation; not assigned per trade')


def operating_costs(records, coverage, start, end):
    """Accrue verified service-period costs linearly over [start,end).

    Each record needs USD gross including tax, NWT allocation fraction, source
    and mapping references. Reconciled coverage is declared for every service,
    including verified zero/non-applicability. Estimates are retained separately
    and can never establish completeness. Invoice is expense, not proof of payment.
    """
    start, end = stamp(start), stamp(end)
    if end <= start:
        raise ValueError('Positive accounting window required')
    known = Decimal(0); estimates = Decimal(0); gaps = []; included = []
    for r in unique(records):
        service = r.get('service')
        if service not in REQUIRED_SERVICES:
            raise ValueError('Unapproved service allocation')
        a, b = stamp(r['period_start']), stamp(r['period_end'])
        if b <= a:
            raise ValueError('Invalid service period')
        seconds = Decimal(str(max(0, (min(end,b)-max(start,a)).total_seconds())))
        if not seconds:
            continue
        if (r.get('currency') != 'USD' or r.get('gross_amount') is None or
            r.get('allocation_fraction') is None or not r.get('mapping_reference') or not r.get('source_reference') or
            r.get('tax_included') is not True):
            gaps.append('unverified_cost:' + r['source_id']); continue
        fraction = number(r['allocation_fraction']); amount = number(r['gross_amount'])
        if not 0 <= fraction <= 1 or amount < 0:
            raise ValueError('Invalid allocation or gross amount')
        allocated = amount * fraction * seconds / Decimal(str((b-a).total_seconds()))
        verified = r.get('basis') == 'verified_service_expense'
        if verified: known += allocated
        else:
            estimates += allocated
            gaps.append('estimate_only:' + r['source_id'])
        included.append(dict(source_id=r['source_id'],service=service,allocated_usd=allocated,basis=r.get('basis')))
    for service in REQUIRED_SERVICES:
        c = coverage.get(service, {})
        if not (c.get('reconciled') is True and c.get('source_reference') and c.get('mapping_reference') and
                c.get('period_start') and c.get('period_end') and
                stamp(c['period_start']) <= start and stamp(c['period_end']) >= end and
                c.get('currency') == 'USD'):
            gaps.append('coverage:' + service)
        elif not any(x['service']==service and x['basis']=='verified_service_expense' for x in included) and not c.get('verified_zero_or_not_applicable'):
            gaps.append('missing_expense_records:' + service)
    return dict(complete=not gaps,currency='USD',window_start=start.isoformat(),window_end=end.isoformat(),
                known_accrued_usd=known,estimated_accrued_usd=estimates,
                accrued_usd=known if not gaps else None,missing=sorted(set(gaps)),records=included,
                allocation_rule='explicit NWT fraction times UTC service-period overlap; no arbitrary per-trade allocation')


def account_net(start_equity, end_equity, activities, costs, *, activity_coverage_complete):
    flows=Decimal(0); gaps=[]
    for a in unique(activities):
        if a.get('currency') != 'USD':
            gaps.append('activity_currency:' + a['source_id']); continue
        if a['kind']=='external_cash': flows+=number(a['amount'])
        elif a['kind'] not in ('fill','fee','dividend','interest'):
            gaps.append('unclassified_activity:' + a['source_id'])
    if not activity_coverage_complete: gaps.append('incomplete_activity_history')
    pnl=number(end_equity)-number(start_equity)-flows if not gaps else None
    return dict(trading_pnl_usd=pnl,external_cash_usd=flows,missing=gaps,
                net_after_operating_costs_usd=pnl-costs['accrued_usd'] if pnl is not None and costs['complete'] else None,
                fee_treatment='Broker fees/dividends/interest already in equity; no second deduction')
