from unittest.mock import MagicMock,patch
from datetime import datetime,timezone
import pytest
import engine


def test_budget_failure_prevents_reservation_and_order_submission():
    ticket=dict(ticket_id='11111111-1111-1111-1111-111111111111',created_at=datetime.now(timezone.utc),
        payload=dict(approved=True,bot_source='EU_BOT',strategy_id='EU-MR-001',symbol='VGK',direction='long',
            asset_type='equity',sized_notional=2000,time_in_force='day'))
    with patch.object(engine,'require_operations_health'),patch.object(engine,'synchronous_risk_veto',return_value=(False,'')), \
         patch('strategy_review.entry_gate',return_value='LIMITED_EXPERIMENT'),patch.object(engine,'insert_decision') as decision, \
         patch.object(engine,'mark_decision_outcome'),patch.object(engine,'reserve') as reserve,patch.object(engine,'alpaca_post') as post:
        engine.process_ticket(MagicMock(),ticket,{})
    reserve.assert_not_called();post.assert_not_called()
    assert 'STRATEGY_BUDGET' in decision.call_args[0][3]


def test_limited_equity_order_has_enforceable_maximum_notional():
    p=dict(symbol='VGK',sized_notional=500,direction='long',time_in_force='day',_experiment_budget=500)
    with patch.object(engine,'get_current_price',return_value=87),patch.object(engine,'submit_identified_order',side_effect=lambda x:x):
        order=engine.place_equity_order(p)
    assert order['type']=='limit' and float(order['qty'])*float(order['limit_price'])<=500
    with patch.object(engine,'get_current_price',return_value=501):
        with pytest.raises(ValueError,match='One share'):engine.place_equity_order(p)
