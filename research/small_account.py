"""Illustrative $5,000 feasibility checks, never order-sizing instructions."""
from math import gcd
from functools import reduce


def assess(legs,capital=5000):
    result={'capital':capital,'illustrative_risk_budget':capital*.02,'eligible':False}
    if not legs:return dict(result,reason='Missing legs')
    if all(x['asset_type']=='equity' for x in legs):
        if len(legs)!=1 or legs[0]['direction']!='long':return dict(result,reason='Short margin or basket feasibility not established')
        price=float(legs[0]['entry_price']);stop=legs[0].get('stop_pct')
        return dict(result,eligible=price<=capital*.1 and stop is not None and price*abs(float(stop))<=capital*.02,
                    minimum_whole_share_cost=price,estimated_stop_loss=price*abs(float(stop)) if stop is not None else None,
                    reason='One whole share; stop loss can gap, and costs are additional')
    if not all(x['asset_type']=='option' for x in legs):return dict(result,reason='Mixed instruments unsupported')
    try:
        qty=[int(x['qty']) for x in legs]
        if any(q<=0 or float(x['qty'])!=q for q,x in zip(qty,legs)):raise ValueError()
        unit=reduce(gcd,qty)
        parsed=[]
        for x,q in zip(legs,qty):
            symbol=x['asset'];kind=symbol[-9];strike=int(symbol[-8:])/1000
            if kind not in ('C','P'):raise ValueError()
            parsed.append((symbol[:-15],symbol[-15:-9],kind,strike,(1 if x['direction']=='long' else -1)*q/unit,float(x['entry_price'])))
        if len({x[:2] for x in parsed})!=1:return dict(result,reason='Different expiries or underlying assets need separate risk modelling')
        if sum(x[4] for x in parsed if x[2]=='C')<0:return dict(result,reason='Unbounded upside loss')
        debit=sum(x[4]*x[5]*100 for x in parsed)
        points=[0]+[x[3] for x in parsed]+[max(x[3] for x in parsed)*2]
        payoffs=[sum(x[4]*100*(max(spot-x[3],0) if x[2]=='C' else max(x[3]-spot,0)) for x in parsed) for spot in points]
        risk=max(0,debit-min(payoffs))
        return dict(result,eligible=risk<=capital*.02 and max(debit,0)<=capital*.1,
                    minimum_structure_expiry_loss=risk,minimum_structure_debit=debit,
                    reason='Expiry payoff only; commissions, early assignment, legging, broker approval and buying-power checks still required')
    except (KeyError,ValueError,TypeError):return dict(result,reason='Incomplete contract evidence')
