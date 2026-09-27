"""Human-readable operational reports; record counts are not trade counts."""
from datetime import date
from decimal import Decimal, InvalidOperation


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite(): return 'unavailable'
        return ('-' if amount < 0 else '') + f'${abs(amount):,.2f}'
    except (InvalidOperation, ValueError, TypeError):
        return 'unavailable'


def format_report(status, trial, day, activation=False):
    issues = status.get('issues', [])
    warnings = status.get('research_warnings', [])
    lines = [
        'NWT supervision activated' if activation else 'NWT daily paper report',
        'Reporting date: ' + date.fromisoformat(day).strftime('%d %b %Y') + ' (UTC)',
        'Status: ' + ('Needs attention' if issues else 'Trading checks healthy; research needs attention' if warnings else 'Healthy'),
        'US market: ' + ('open' if status.get('market_open') else 'closed'),
        '',
        f"Trial: {trial.get('consecutive_passes', 0)}/{trial.get('required_sessions', 20)} consecutive sessions",
        'Open paper positions: ' + str(status.get('broker_positions', 'unavailable')),
        'Pending broker orders: ' + str(status.get('open_orders', 'unavailable')),
        'Broker paper equity (including open positions): ' + money(status.get('broker_equity')),
    ]
    if status.get('session_incidents'):
        lines.append('This session had incidents; recovery does not count it as a clean trial day.')
    if warnings:
        lines += ['Research warnings (unrelated paper entries not held): ' + '; '.join(warnings)]
    decisions = status.get('decisions') or []
    if decisions:
        lines += ['', 'Decision records:']
        for row in decisions:
            label = str(row.get('decision', 'unknown')).replace('_', ' ').capitalize()
            lines.append(f"• {label}: {row.get('n', 0)}")
    outcomes = status.get('outcomes') or {}
    discovery = (status.get('research') or {}).get('discovery') or {}
    if discovery:
        patterns = discovery.get('patterns') or {}
        lines += ['', 'Paper research account: $100,000 starting capital',
                  f"Market coverage: {discovery.get('symbols_received', 0)} symbols; {discovery.get('option_contracts', 0)} option contracts",
                  f"Minute bars stored: {patterns.get('minute_bars', 0):,}",
                  f"Pattern signals: {patterns.get('triggered_signals', 0)}; evaluated horizons: {patterns.get('outcomes', 0)}",
                  'Pattern results are research estimates, separate from broker profit.']
        board = patterns.get('scoreboard') or []
        lines.append(f"Research sessions observed: {patterns.get('observed_sessions', 'unavailable')}")
        if board:
            positive = sum(float(r['mean_after_30bps']) > 0 for r in board)
            lines.append(f'Positive average after 30-basis-point cost stress: {positive}/{len(board)} rule/horizon combinations.')
            best = max(board, key=lambda r: float(r['mean_after_30bps']))
            lines.append(f"Highest stressed average: {best['rule']} / {best['horizon_minutes']} min: {float(best['mean_after_30bps']):+.3%} ({best['samples']} overlapping observations).")
        else:
            lines.append('No evaluated research results available yet.')
        experiments = discovery.get('experiments') or {}
        if experiments:
            lines.append(f"New experiment cycle: {experiments.get('sessions', 0)} sessions; review candidates: {sum(x.get('state') == 'REVIEW_CANDIDATE' for x in experiments.get('rules', []))}.")
        lines += ['Overlapping observations are not independent trades.',
                  'These exploratory results do not establish a profitable strategy or live readiness.']
    attribution = ((status.get('research') or {}).get('learning_review') or {}).get('attribution') or {}
    daily = (status.get('research') or {}).get('daily_comparison') or {}
    if daily:
        lines += ['', 'Daily equity comparison (modeled, not broker profit; 30bps round-trip costs):']
        portfolios = daily.get('portfolios') or []
        benchmarks = {p['symbol']: p['equity'] for p in portfolios if p['rule'] == 'buy_hold'}
        for p in portfolios:
            excess = p['equity']-benchmarks.get(p['symbol'], p['equity'])
            lines.append(f"{p['symbol']} {p['rule']}: {p['equity']-1:+.2%}; vs hold {excess:+.2%} points; drawdown {p['drawdown']:.2%}; {p['sessions']} sessions.")
        lines.append('Prospective only; no automatic strategy promotion.')
    if attribution:
        lines += ['', 'Complete-trade attribution since 15 Sep:']
        for row in attribution.get('strategies', [])[:8]:
            lines.append(f"{row['strategy']}: {row['complete_trades']} complete trades; adjusted {money(row['net'])}")
        lines.append('Open/incomplete groups excluded: ' + str(len(attribution.get('excluded_groups', []))))
    lines += ['', 'Recorded outcome rows: ' + str(outcomes.get('n', 'unavailable')),
              'Adjusted result for these records: ' + money(outcomes.get('net')),
              'Record counts can include individual option legs; they are not counts of complete trades.',
              '', 'Learning records available: ' + str(status.get('learning_outcome_rows', 'unavailable')),
              'Strategy changes remain frozen pending qualifying evidence.', '',
              'Action needed: ' + '; '.join(issues) if issues else 'Research recovery is being monitored; trading checks are healthy.' if warnings else 'No operational action needed; profitability remains under evaluation.']
    return '\n'.join(lines)
