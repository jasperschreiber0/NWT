from decimal import Decimal
import pytest
from cost_accounting import trade_costs, operating_costs, account_net, REQUIRED_SERVICES

START='2026-10-04T00:00:00Z'; END='2026-10-20T00:00:00Z'


def fill(i,phase,qty,price):
    return dict(source_id=i,instrument='SPY',phase=phase,side='buy' if phase=='entry' else 'sell',
                qty=qty,price=price,multiplier=1,currency='USD',quantity_kind='incremental',
                executed_at='2026-10-05T12:00:00Z' if phase=='entry' else '2026-10-06T12:00:00Z',fee_usd='.10')


def test_partial_executions_replayed_once_no_spread_double_charge():
    a=fill('a','entry',3,100); b=fill('b','exit',1,102); c=fill('c','exit',2,103)
    kw=dict(fees_complete=True,fee_manifest='fees-export-hash',expected_instruments=['SPY'],fill_manifest='fills-export-hash',stress_usd=2)
    r=trade_costs([a,b,c,a,b],**kw)
    assert r==trade_costs([a,b,c],**kw)
    assert r['gross_fill_pnl']==8 and r['execution_net_usd']==Decimal('7.70')
    assert r['stress_pnl_usd']==6 and r['fully_loaded_net_usd'] is None
    assert trade_costs([a,b],**kw)['execution_net_usd'] is None
    assert trade_costs([a,b,c])['execution_net_usd'] is None
    with pytest.raises(ValueError):trade_costs([a,dict(a,qty=9)],**kw)
    with pytest.raises(ValueError):trade_costs([dict(a,quantity_kind='cumulative')],**kw)


def record():
    return dict(source_id='invoice-line-1',service='hetzner',currency='USD',gross_amount='16',tax_included=True,
                allocation_fraction='.5',source_reference='invoice-sha',mapping_reference='resource-map-sha',
                period_start=START,period_end=END,basis='verified_service_expense')


def coverage():
    return {s:dict(reconciled=True,source_reference='manifest-'+s,mapping_reference='mapping-'+s,
                   period_start=START,period_end=END,currency='USD',verified_zero_or_not_applicable=s!='hetzner')
            for s in REQUIRED_SERVICES}


def test_service_period_tax_allocation_and_unknowns():
    r=record(); c=coverage()
    out=operating_costs([r,r],c,START,END)
    assert out['complete'] and out['accrued_usd']==8
    old=dict(r,period_start='2026-09-01T00:00:00Z',period_end='2026-10-01T00:00:00Z')
    assert not operating_costs([old],c,START,END)['complete']
    assert operating_costs([dict(r,currency='$')],c,START,END)['accrued_usd'] is None
    assert operating_costs([dict(r,basis='usage_estimate')],c,START,END)['accrued_usd'] is None
    assert not operating_costs([r],{},START,END)['complete']
    with pytest.raises(ValueError):operating_costs([dict(r,service='kaspr')],c,START,END)
    with pytest.raises(ValueError):operating_costs([dict(r,allocation_fraction=2)],c,START,END)


def test_cash_flows_not_profit_and_broker_fees_not_subtracted_twice():
    costs=operating_costs([record()],coverage(),START,END)
    deposit=dict(source_id='cash-1',currency='USD',kind='external_cash',amount=1000)
    fee=dict(source_id='fee-1',currency='USD',kind='fee',amount=-1)
    result=account_net(10000,11020,[deposit,deposit,fee],costs,activity_coverage_complete=True)
    assert result['trading_pnl_usd']==20 and result['net_after_operating_costs_usd']==12
    assert account_net(1,2,[],costs,activity_coverage_complete=False)['trading_pnl_usd'] is None
    assert account_net(1,2,[dict(fee,kind='transfer_unknown')],costs,activity_coverage_complete=True)['trading_pnl_usd'] is None
