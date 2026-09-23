"""Validate cumulative broker entry evidence; never substitute market quotes."""
import uuid
from decimal import Decimal
from fill_recovery import fill_data


def entry_rows(ticket,order):
    payload=ticket['payload'];tid=str(ticket['ticket_id'])
    if not payload.get('legs'):
        return [fill_data(ticket,order)]
    if order.get('client_order_id')!='nwt-entry-'+tid or order.get('order_class')!='mleg':
        raise ValueError('Multi-leg broker identity mismatch')
    expected={x['option_symbol']:x for x in payload['legs']}
    actual={x['symbol']:x for x in order.get('legs') or []}
    if len(expected)!=len(payload['legs']) or len(actual)!=len(order.get('legs') or []) or set(expected)!=set(actual):
        raise ValueError('Missing or duplicate broker leg receipts')
    group=str(uuid.uuid5(uuid.NAMESPACE_URL,'nwt-spread:'+tid))
    rows=[]
    for symbol,leg in expected.items():
        receipt=actual[symbol]
        if Decimal(str(receipt.get('qty') or 0)) != Decimal(str(order.get('qty') or 0)) * Decimal(str(leg.get('ratio_qty',1))):
            raise ValueError('Spread leg requested quantity mismatch')
        if receipt.get('side')!=leg['side']:raise ValueError('Spread leg direction mismatch')
        if Decimal(str(receipt.get('filled_qty') or 0))==0:continue
        # Reuse strict single-instrument price/quantity/timestamp validation.
        p={k:v for k,v in payload.items() if k!='legs'}
        p.update(asset_type='equity',symbol=symbol,direction='long' if leg['side']=='buy' else 'short')
        r=dict(receipt,client_order_id='nwt-entry-'+tid,order_class='simple')
        data=fill_data({'ticket_id':tid,'payload':p},r)
        data.update(asset_type='option',spread_group_id=group,alpaca_order_id=order['id'],
                    notional_risk=data['qty']*data['entry_price']*100,
                    delta_exposure=(.5 if leg.get('option_type','call')=='call' else -.5)*(1 if leg['side']=='buy' else -1))
        rows.append(data)
    if order.get('status')=='filled' and len(rows)!=len(expected):raise ValueError('Filled spread has missing leg fills')
    return rows
