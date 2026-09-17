from decimal import Decimal
from report_format import format_report, money


def test_database_results_are_readable_and_not_called_trades():
    text = format_report(dict(issues=[], market_open=False, broker_positions=6, open_orders=0,
        decisions=[dict(decision='EXECUTED', n=12)], outcomes=dict(n=3, net=Decimal('-771.5991')),
        learning_outcome_rows=52), dict(consecutive_passes=0, required_sessions=20), '2026-09-14')
    assert '-$771.60' in text and '• Executed: 12' in text
    assert 'Decimal' not in text and 'RealDictRow' not in text
    assert 'not counts of complete trades' in text
    assert '14 Sep 2026 (UTC)' in text and 'US market: closed' in text


def test_missing_or_nonfinite_results_are_not_reported_as_zero():
    assert money(None) == 'unavailable'
    assert money('NaN') == 'unavailable'
    assert money('0') == '$0.00'



def test_research_cost_results_and_overlap_are_disclosed():
    text=format_report({'issues':[], 'session_incidents':1, 'research':{'discovery':{'patterns':{
        'observed_sessions':3,'scoreboard':[{'rule':'test','horizon_minutes':30,'samples':1,
        'mean_after_10bps':.001,'mean_after_30bps':-.001}]}}}}, {},'2026-09-17')
    assert '0/1 rule/horizon combinations' in text
    assert '-0.100%' in text
    assert 'not independent trades' in text
    assert 'Research sessions observed: 3' in text
    assert 'does not count it as a clean trial day' in text
