"""Daily evidence report: realized learning and shadow research stay distinct."""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'nwt_agents'))
load_dotenv(ROOT/'nwt_agents/.env')
from shared_context import get_db
from psycopg2.extras import RealDictCursor


def main():
    c=get_db()
    try:
        with c.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("SELECT o.strategy_id,COUNT(DISTINCT COALESCE(l.spread_group_id,l.position_id)) trades,"
                      "SUM(o.pnl_adjusted) net FROM nwt_trade_outcomes o JOIN nwt_portfolio_ledger l "
                      "ON l.position_id=o.position_id GROUP BY o.strategy_id")
            realized=[dict(x) for x in q.fetchall()]
            q.execute("SELECT strategy_id,genome_version,COUNT(*) observations,"
                      "COUNT(*) FILTER (WHERE shadow_evaluated_at IS NOT NULL) evaluated "
                      "FROM nwt_decision_inputs GROUP BY strategy_id,genome_version")
            shadow=[dict(x) for x in q.fetchall()]
            q.execute("SELECT strategy_id,version,parent_version FROM nwt_strategy_genome WHERE shadow_mode AND NOT active")
            candidates=[dict(x) for x in q.fetchall()]
        from profit_attribution import build
        import os, requests
        base=os.environ['NWT_ALPACA_BASE_URL'].rstrip('/')
        if base!='https://paper-api.alpaca.markets':raise RuntimeError('Paper endpoint required')
        session=requests.Session()
        def get(path):
            response=session.get(base+'/v2'+path,headers={'APCA-API-KEY-ID':os.environ['NWT_ALPACA_KEY_ID'],
                'APCA-API-SECRET-KEY':os.environ['NWT_ALPACA_SECRET_KEY']},timeout=15)
            response.raise_for_status();return response.json()
        attribution = build(c,get)
        from strategy_review import load_rows, review, latch
        strategy_review = review(load_rows(c))
        strategy_review['strategies']=latch(c,strategy_review['strategies'])
        data=dict(attribution=attribution, observed_at=datetime.now(timezone.utc).isoformat(),realized=realized,
                  strategy_review=strategy_review,
                  underlying_proxy_observations=shadow,pending_candidates=candidates,
                  promotion_policy='No option promotion from underlying proxies; require execution-grade shadow evidence, existing sample/regime gates and completed reliability trial.',
                  outcome='learning_report_complete')
        p=Path('/var/lib/nwt-ops/learning.json');tmp=p.with_suffix('.tmp')
        archive=p.parent/'strategy-reviews';archive.mkdir(exist_ok=True)
        snapshot=archive/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json')
        with snapshot.open('x') as f:json.dump(strategy_review,f,default=str,indent=2)
        tmp.write_text(json.dumps(data,default=str,indent=2));tmp.replace(p)
        print(json.dumps(data,default=str))
    finally:c.close()


if __name__=='__main__':main()
