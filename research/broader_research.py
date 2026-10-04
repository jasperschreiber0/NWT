"""Separate frozen stock and cross-asset studies; original ETF study is untouched."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASKETS={'stocks':['SPY','QQQ','AAPL','MSFT','NVDA','AMZN','META','GOOGL'],
         'macro':['SPY','QQQ','GLD','TLT','HYG','IWM','EEM','VGK']}


def study(name):
    specification=importlib.util.spec_from_file_location('nwt_lab_'+name,ROOT/'research/strategy_lab.py')
    module=importlib.util.module_from_spec(specification);specification.loader.exec_module(module)
    module.SYMBOLS=list(BASKETS[name]);module.POLICY=copy.deepcopy(module.POLICY)
    module.POLICY.update(version='broader-'+name+'-20261005-v1',symbols=module.SYMBOLS,
        basket_semantics='sector_strength is relative leadership within this fixed declared basket',
        adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    module.POLICY['limitations'].append('Survivor-selected basket; publication of historic theories is not a pristine holdout')
    original=module.catalog
    def catalog():
        specs=original()
        for spec in specs:
            if spec['family']=='sector_strength':spec['rationale']='Relative strength within the declared '+name+' basket'
        return specs
    module.catalog=catalog
    return module


def run(out,get,calendar,now,latest):
    from universe import paged
    out.mkdir(parents=True,exist_ok=True);cache=out/('bars-'+now.date().isoformat()+'.json')
    symbols=sorted(set(s for basket in BASKETS.values() for s in basket))
    data=json.loads(cache.read_text()) if cache.exists() else {}
    if not data or any(not data.get(s) or data[s][-1]['t'][:10]!=latest for s in symbols):
        data=paged(get,'https://data.alpaca.markets/v2/stocks/bars',dict(symbols=','.join(symbols),timeframe='1Day',
            start='2016-01-01',end=now.isoformat(),feed='sip',adjustment='all',limit=10000),'bars',30)['bars']
        data={s:[b for b in rows if b['t'][:10]<=latest] for s,rows in data.items()}
        from research_hub import atomic
        atomic(cache,data)
    reports={}
    for name in BASKETS:
        module=study(name)
        reports[name]=module.run(out/name,{s:data[s] for s in BASKETS[name]},calendar,now)
    return dict(status='OK' if all(x['status']=='OK' for x in reports.values()) else 'DEGRADED',
        observed_at=now.isoformat(),studies=reports,coverage={s:dict(first=data[s][0]['t'],last=data[s][-1]['t'],bars=len(data[s])) for s in symbols},
        note='Equity data cannot reconstruct option prices; existing option and event lanes retain their own evidence requirements')
