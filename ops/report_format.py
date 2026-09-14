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
    lines += ['', 'Recorded outcome rows: ' + str(outcomes.get('n', 'unavailable')),
              'Adjusted result for these records: ' + money(outcomes.get('net')),
              'Record counts can include individual option legs; they are not counts of complete trades.',
              '', 'Learning records available: ' + str(status.get('learning_outcome_rows', 'unavailable')),
              'Strategy changes remain frozen pending qualifying evidence.', '',
              'Action needed: ' + '; '.join(issues) if issues else 'No action needed.']
    return '\n'.join(lines)
