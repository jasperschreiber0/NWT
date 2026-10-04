"""Bounded autonomous hypothesis search and prospective model portfolio.

No network, LLM-generated executable code, broker orders, or capital authority.
Every trial is registered; historical evidence is explicitly retrospective.
"""
import hashlib
import itertools
import json
import math
import sqlite3
import statistics as stats
from datetime import timedelta

from event_core import sessions, dt
from research_engine import SYMBOLS

POLICY = {
    'version': 'strategy-lab-20261005-v1', 'symbols': SYMBOLS,
    'families': {'momentum': [20, 60, 120], 'pullback': [3, 5, 10],
                 'breakout': [20, 40, 60], 'sector_strength': [20, 60, 120]},
    'filters': ['none', 'trend200', 'calm25'], 'hold_sessions': 5,
    'cost_per_side': .0015, 'stress_cost_per_side': .003,
    'historical_periods': {'development': ['2017-01-01', '2019-12-31'],
                           'validation': ['2020-01-01', '2022-12-31'],
                           'audit': ['2023-01-01', '2025-12-31']},
    'combination_selection': 'Best development mean excess per family; six equal-weight cross-family pairs',
    'review_cohorts': 12, 'minimum_active': 6, 'excess_standard_errors': 4,
    'max_drawdown': .10, 'max_correlation': .75,
    'allocation': 'Maximum three different families, up to 20% virtual capital each; reduce with declining last-12-cohort stressed mean; remainder cash',
    'research_capital': 100000, 'small_account_reference': 5000,
    'promotion': 'Automatic virtual-paper allocation only; no broker order authority',
    'limitations': ['Historical windows have already been inspected; all historical tests exploratory',
                   'Fixed surviving ETFs; no delisted-stock universe or verified 40-year history',
                   'Four-standard-error hurdle is a conservative heuristic, not calibrated multiple-test significance',
                   'Adjusted-price fractional model, not actual fills; operating bills excluded',
                   'Twelve weekly cohorts are limited evidence; cross-model returns are dependent'],
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def get(c, kind, key):
    row = c.execute('SELECT payload FROM records WHERE kind=? AND key=?', (kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def put(c, kind, key, value):
    old = get(c, kind, key)
    if old is not None and old != value:
        raise ValueError('Frozen research record changed: ' + kind + ':' + key)
    c.execute('INSERT OR IGNORE INTO records VALUES (?,?,?)', (kind, key, encoded(value)))


def records(c, kind):
    return [json.loads(r[0]) for r in c.execute('SELECT payload FROM records WHERE kind=? ORDER BY key', (kind,))]


def catalog():
    result = []
    for family, windows in POLICY['families'].items():
        for window, condition in itertools.product(windows, POLICY['filters']):
            key = f'{family}-{window}-{condition}'
            result.append(dict(id=key, family=family, window=window, condition=condition,
                               parents=[f'{family}-{window}-none'] if condition != 'none' else [],
                               rationale={'momentum': 'Persistence of relative returns',
                                          'pullback': 'Short reversal after a recent decline',
                                          'breakout': 'Continuation after a closing-price range break',
                                          'sector_strength': 'Persistence of sector leadership'}[family]))
    return result


def validate(data):
    indexed = {}
    for symbol in SYMBOLS:
        rows = data[symbol]
        days = [b['t'][:10] for b in rows]
        if days != sorted(set(days)):
            raise ValueError('Unsorted or duplicate bars: ' + symbol)
        if any(not math.isfinite(float(b[k])) or float(b[k]) <= 0 for b in rows for k in ('o', 'c')):
            raise ValueError('Invalid prices: ' + symbol)
        indexed[symbol] = {b['t'][:10]: b for b in rows}
    days = list(indexed['SPY'])
    if len(days) < 253 or any(list(indexed[s]) != days for s in SYMBOLS):
        raise ValueError('Insufficient or unaligned history')
    return days, indexed


def targets(spec, history, specs):
    if spec['family'] == 'combination':
        merged = {}
        for parent in spec['parents']:
            for s, w in targets(specs[parent], history, specs).items():
                merged[s] = merged.get(s, 0) + w / len(spec['parents'])
        return merged
    spy = history['SPY']
    if len(spy) < 200:
        return {}
    if spec['condition'] == 'trend200' and spy[-1] <= stats.mean(spy[-200:]):
        return {}
    returns = [spy[i]/spy[i-1]-1 for i in range(len(spy)-20, len(spy))]
    if spec['condition'] == 'calm25' and stats.stdev(returns) * 252**.5 >= .25:
        return {}
    n = spec['window']; family = spec['family']
    universe = SYMBOLS[2:] if family == 'sector_strength' else SYMBOLS
    ranked = []
    for s in universe:
        prices = history[s]; momentum = prices[-1]/prices[-n-1]-1
        if family in ('momentum', 'sector_strength') and momentum > 0:
            ranked.append((momentum, s))
        elif family == 'pullback' and momentum < 0:
            ranked.append((-momentum, s))
        elif family == 'breakout' and prices[-1] > max(prices[-n-1:-1]):
            ranked.append((momentum, s))
    return {s: .5 for _, s in sorted(ranked, reverse=True)[:2]}


def outcome(weights, indexed, entry, end, days):
    """Next-open to fifth-next-open, with both executable-side cost assumptions."""
    cost = POLICY['cost_per_side']; stress = POLICY['stress_cost_per_side']
    gross = sum(weights.values())
    if not 0 <= gross <= 1.00000001 or any(w < 0 for w in weights.values()):
        raise ValueError('Invalid unlevered allocation')
    def net(s, fee):
        ratio = float(indexed[s][end]['o']) / float(indexed[s][entry]['o'])
        # Entry cost comes from allocated cash, never implicit borrowing.
        return ratio * (1-fee)/(1+fee) - 1
    value = sum(w * net(s, cost) for s, w in weights.items())
    stressed = sum(w * net(s, stress) for s, w in weights.items())
    benchmark = gross * net('SPY', cost)
    # Mark every intermediate close; include opening gaps and final exit cost.
    path = []
    for day in days[days.index(entry):days.index(end)]:
        for price in ('o', 'c'):
            path.append(sum(w * (float(indexed[s][day][price])/float(indexed[s][entry]['o'])/(1+cost)-1)
                            for s, w in weights.items()))
    path.append(value)
    peak = 1.; drawdown = 0.
    for point in path:
        peak = max(peak, 1+point); drawdown = max(drawdown, 1-(1+point)/peak)
    return dict(entry=entry, exit=end, net=value, stress_net=stressed, benchmark=benchmark,
                excess=value-benchmark, gross=gross, drawdown=drawdown, path=path)


def summary(rows):
    if not rows:
        return dict(cohorts=0, active=0, mean=0., excess=0., drawdown=0., compounded=0.)
    equity = peak = 1.; dd = 0.
    for row in rows:
        for point in row.get('path', [row['net']]):
            mark = equity * (1+point); peak = max(peak, mark); dd = max(dd, 1-mark/peak)
        equity *= 1+row['net']
    return dict(cohorts=len(rows), active=sum(r['gross'] > 0 for r in rows),
                mean=stats.mean(r['net'] for r in rows), excess=stats.mean(r['excess'] for r in rows),
                stress_mean=stats.mean(r['stress_net'] for r in rows), drawdown=dd, compounded=equity-1)


def historical(spec, specs, data, days, indexed):
    result = {}
    for name, (start, end) in POLICY['historical_periods'].items():
        eligible = [i for i, d in enumerate(days) if start <= d <= end and i >= 253]
        rows = []
        for i in eligible[::POLICY['hold_sessions']]:
            j = i + POLICY['hold_sessions']
            if j >= len(days) or days[j] > end:
                continue
            h = {s: [float(b['c']) for b in data[s][max(0, i-253):i]] for s in SYMBOLS}
            w = targets(spec, h, specs)
            rows.append(outcome(w, indexed, days[i], days[j], days))
        result[name] = summary(rows)
    return result


def initialize(c, data, days, indexed, now):
    old = get(c, 'policy', POLICY['version'])
    if old and old['policy'] != POLICY:
        raise ValueError('Frozen lab policy changed without a version bump')
    if old and old['code_sha256'] != hashlib.sha256(__file_bytes()).hexdigest():
        raise ValueError('Frozen experiment code changed; start a separately versioned study')
    if old:
        return
    put(c, 'policy', POLICY['version'], dict(policy=POLICY, frozen_at=now.isoformat(),
        data_sha256=digest(data), code_sha256=hashlib.sha256(__file_bytes()).hexdigest()))
    specs = {s['id']: s for s in catalog()}
    reviews = {}
    # Register all primitives before reading their outcomes.
    for spec in specs.values():
        put(c, 'spec', spec['id'], spec)
    for key, spec in specs.items():
        reviews[key] = historical(spec, specs, data, days, indexed)
        put(c, 'history', key, reviews[key])
    winners = []
    for family in POLICY['families']:
        group = [s for s in specs.values() if s['family'] == family]
        winners.append(max(group, key=lambda s: (reviews[s['id']]['development']['excess'], s['id']))['id'])
    for a, b in itertools.combinations(winners, 2):
        spec = dict(id='blend-'+digest([a, b])[:12], family='combination', parents=[a, b],
                    rationale='Equal-weight combination selected using development evidence only')
        put(c, 'spec', spec['id'], spec)
        result = historical(spec, specs, data, days, indexed)
        # Ablation: retain comparisons with BOTH simpler components.
        result['parent_excess'] = {p: {period: result[period]['mean']-reviews[p][period]['mean']
            for period in POLICY['historical_periods']} for p in spec['parents']}
        put(c, 'history', spec['id'], result)


def __file_bytes():
    from pathlib import Path
    return Path(__file__).read_bytes()


def evaluate(rows, parent_rows):
    n = POLICY['review_cohorts']; ordered = sorted(rows, key=lambda x: x['entry'])[:n]
    if len(ordered) < n:
        return dict(state='COLLECTING', completed=len(ordered), required=n)
    metrics = summary(ordered)
    def lower(values):
        return stats.mean(values)-POLICY['excess_standard_errors']*stats.stdev(values)/len(values)**.5
    comparisons = {'exposure_matched_spy': [r['excess'] for r in ordered]}
    for parent, others in parent_rows.items():
        matched = {r['entry']: r for r in others}
        if any(r['entry'] not in matched for r in ordered):
            return dict(state='COLLECTING', reason='Missing paired parent results', completed=n, required=n)
        comparisons[parent] = [r['net']-matched[r['entry']]['net'] for r in ordered]
    bounds = {k: lower(v) for k, v in comparisons.items()}
    reasons = []
    if metrics['active'] < POLICY['minimum_active']: reasons.append('Too few active cohorts')
    if metrics['stress_mean'] <= 0: reasons.append('Nonpositive stressed net return')
    if metrics['drawdown'] > POLICY['max_drawdown']: reasons.append('Drawdown limit exceeded')
    if min(bounds.values()) <= 0: reasons.append('Incremental edge hurdle failed')
    return dict(state='RETIRED' if reasons else 'PAPER_QUALIFIED', completed=n, required=n,
                metrics=metrics, lower_heuristics=bounds, reasons=reasons,
                meaning='Fixed first review; virtual allocation eligibility, not live readiness')


def select_portfolio(verdicts, specs, histories):
    chosen = []; families = set()
    eligible = [k for k, v in verdicts.items() if v['state'] == 'PAPER_QUALIFIED']
    recent = {k: summary(histories[k][-POLICY['review_cohorts']:])['stress_mean'] for k in eligible}
    for key in sorted(eligible, key=lambda k: (-recent[k], k)):
        if recent[key] <= 0: continue
        family = specs[key]['family']
        if family in families:
            continue
        current = {r['entry']: r['net'] for r in histories[key]}
        correlated = False
        for other in chosen:
            previous = {r['entry']: r['net'] for r in histories[other]}
            dates = sorted(set(current) & set(previous))
            if len(dates) < POLICY['review_cohorts']:
                correlated = True; break
            x = [current[d] for d in dates]; y = [previous[d] for d in dates]
            if stats.pstdev(x) == 0 or stats.pstdev(y) == 0 or abs(stats.correlation(x, y)) >= POLICY['max_correlation']:
                correlated = True; break
        if not correlated:
            chosen.append(key); families.add(family)
        if len(chosen) == 3: break
    return {k: .2*min(1., recent[k]/verdicts[k]['metrics']['stress_mean']) for k in chosen}


def adjudicate(c, key, rows, parents, now):
    verdict = get(c, 'verdict', key)
    if not verdict:
        verdict = evaluate(rows, parents)
        if verdict['state'] != 'COLLECTING': put(c, 'verdict', key, verdict)
    if verdict['state'] == 'PAPER_QUALIFIED':
        later = rows[POLICY['review_cohorts']:]
        if len(later) >= 3 and (summary(later)['compounded'] <= -.05 or summary(later)['drawdown'] > .10):
            put(c, 'demotion', key, get(c, 'demotion', key) or dict(state='DEMOTED',
                reason='Post-qualification loss budget exceeded', observed_at=now.isoformat()))
        if get(c, 'demotion', key): verdict = get(c, 'demotion', key)
    return verdict


def run(folder, data, calendar, now):
    folder.mkdir(parents=True, exist_ok=True)
    days, indexed = validate(data)
    cal = sessions(calendar)
    complete = [s for s in cal if dt(s['close'])+timedelta(minutes=15) <= now]
    if not complete or days[-1] != complete[-1]['date']:
        raise ValueError('Lab requires latest completed session and no future bars')
    future = [s for s in cal if dt(s['open']) > now+timedelta(minutes=2)]
    c = sqlite3.connect(folder/'lab.sqlite', timeout=20)
    c.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    with c:
        initialize(c, data, days, indexed, now)
        specs = {s['id']: s for s in records(c, 'spec')}
        errors = []
        for cohort in records(c, 'cohort'):
            entry = cohort['entry']; end = cohort['exit']; key = entry
            if get(c, 'cohort_result', key): continue
            if dt(cohort['frozen_at']) >= dt(cohort['entry_at']):
                raise ValueError('Late cohort registration')
            if end > days[-1]: continue
            if entry not in days or end not in days:
                errors.append('Missing cohort prices: '+entry); continue
            expected = [s['date'] for s in cal if entry <= s['date'] <= end]
            actual = days[days.index(entry):days.index(end)+1]
            if expected != actual or len(actual) != POLICY['hold_sessions']+1:
                raise ValueError('Missing session in cohort')
            results = {k: outcome(w, indexed, entry, end, days) for k, w in cohort['targets'].items()}
            model = outcome(cohort['portfolio_weights'], indexed, entry, end, days)
            put(c, 'cohort_result', key, dict(entry=entry, results=results, portfolio=model,
                source_sha256=digest({s: [indexed[s][d] for d in actual] for s in SYMBOLS}), recorded_at=now.isoformat()))
        all_results = records(c, 'cohort_result')
        histories = {k: [dict(r['results'][k]) for r in all_results if k in r['results']] for k in specs}
        verdicts = {}
        for key, spec in specs.items():
            verdicts[key] = adjudicate(c,key,histories[key],{p:histories[p] for p in spec['parents']},now)
        allocation = select_portfolio(verdicts, specs, histories)
        existing = records(c, 'cohort')
        if future and not errors:
            entry = future[0]['date']; position = cal.index(future[0]); endpos = position+POLICY['hold_sessions']
            last_exit = max((x['exit'] for x in existing), default='')
            if entry >= last_exit and endpos < len(cal) and not get(c, 'cohort', entry):
                h = {s: [float(b['c']) for b in data[s]] for s in SYMBOLS}
                target = {k: targets(s, h, specs) for k, s in specs.items()}
                portfolio = {}
                for key, weight in allocation.items():
                    for symbol, exposure in target[key].items():
                        portfolio[symbol] = portfolio.get(symbol, 0)+weight*exposure
                put(c, 'cohort', entry, dict(entry=entry, exit=cal[endpos]['date'], entry_at=future[0]['open'],
                    frozen_at=now.isoformat(), signal_day=days[-1], data_sha256=digest(data), targets=target,
                    allocation=allocation, portfolio_weights=portfolio))
        model_rows = [r['portfolio'] for r in all_results]
        model_summary = summary(model_rows)
        evidence = {k: dict(spec=specs[k], history=get(c, 'history', k), verdict=verdicts[k]) for k in specs}
        leaders = sorted(specs,key=lambda k:(-evidence[k]['history']['development']['excess'],k))[:3]
        small_weights = {}
        h = {s:[float(b['c']) for b in data[s]] for s in SYMBOLS}
        for key, weight in allocation.items():
            for symbol, exposure in targets(specs[key],h,specs).items():
                small_weights[symbol] = small_weights.get(symbol,0)+weight*exposure
        units = {s:math.floor(POLICY['small_account_reference']*w/(float(data[s][-1]['c'])*(1+POLICY['cost_per_side'])))
                 for s,w in small_weights.items()}
        output = dict(version=POLICY['version'], observed_at=now.isoformat(), status='DEGRADED' if errors else 'OK',
            execution_enabled=False, experiment_count=len(specs), hypotheses=evidence,
            completed_cohorts=len(all_results), pending_cohorts=len(records(c, 'cohort'))-len(all_results),
            states={state: sum(v['state'] == state for v in verdicts.values()) for state in ['COLLECTING', 'RETIRED', 'PAPER_QUALIFIED', 'DEMOTED']},
            allocation=allocation, virtual_cash_weight=1-sum(allocation.values()),
            virtual_portfolio=model_summary, virtual_equity=POLICY['research_capital']*(1+model_summary['compounded']),
            development_leaders=[dict(id=k,history=evidence[k]['history']) for k in leaders],
            small_account=dict(reference=POLICY['small_account_reference'],indicative_whole_units=units,
                note='Latest-close sizing illustration only; not fills or live eligibility'),
            additional_lanes={'bull_put': 'Existing quote-archive experiment; no synthetic option history',
                              'event_ai': 'Existing prospective paired event experiment; no manufactured outcomes'},
            errors=errors, policy=POLICY)
    c.close()
    temporary = folder/'latest.tmp'; temporary.write_text(json.dumps(output, indent=2, allow_nan=False)); temporary.replace(folder/'latest.json')
    return {k: output[k] for k in ['version', 'status', 'experiment_count', 'completed_cohorts', 'pending_cohorts', 'states',
                                  'allocation', 'virtual_cash_weight', 'virtual_equity', 'errors']}
