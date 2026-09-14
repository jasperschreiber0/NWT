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
    lines = [
        'NWT supervision activated' if activation else 'NWT daily paper report',
        'Reporting date: ' + date.fromisoformat(day).strftime('%d %b %Y') + ' (UTC)',
        'Status: ' + ('Needs attention' if issues else 'Healthy'),
        'US market: ' + ('open' if status.get('market_open') else 'closed'),
        '',
        f"Trial: {trial.get('consecutive_passes', 0)}/{trial.get('required_sessions', 20)} consecutive sessions",
        'Open paper positions: ' + str(status.get('broker_positions', 'unavailable')),
        'Pending broker orders: ' + str(status.get('open_orders', 'unavailable')),
    ]
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
    lines += ['', 'Recorded outcome rows: ' + str(outcomes.get('n', 'unavailable')),
              'Adjusted result for these records: ' + money(outcomes.get('net')),
              'Record counts can include individual option legs; they are not counts of complete trades.',
              '', 'Learning records available: ' + str(status.get('learning_outcome_rows', 'unavailable')),
              'Strategy changes remain frozen pending qualifying evidence.', '',
              'Action needed: ' + '; '.join(issues) if issues else 'No action needed.']
    return '\n'.join(lines)
