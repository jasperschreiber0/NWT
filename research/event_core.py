"""Causal, observation-only event primitives. No broker order methods."""
import hashlib,json,math,sqlite3
from datetime import datetime,timezone,timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

UTC=timezone.utc
SYMBOLS=['SPY','QQQ','VGK','EWU','FEZ','FXI','KWEB','MCHI','BABA','EWA','BHP','RIO']

def stamp():return datetime.now(UTC).isoformat()
def dt(s):
    if not s:return None
    try:d=datetime.fromisoformat(s.replace('Z','+00:00'))
    except ValueError:
        try:d=parsedate_to_datetime(s)
        except (ValueError,TypeError):return None
    return d.astimezone(UTC) if d.tzinfo else None

def connect(path):
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row
    c.executescript('''PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,payload TEXT,observed_at TEXT,PRIMARY KEY(kind,key));
    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
    ''');return c

def put(c,kind,key,payload,observed=None):
    cur=c.execute('INSERT OR IGNORE INTO records VALUES (?,?,?,?)',(kind,key,json.dumps(payload,sort_keys=True),observed or stamp()));c.commit();return cur.rowcount==1

def state(c,key):
    row=c.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone();return row[0] if row else None
def setstate(c,key,value):
    c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,value));c.commit()

def parse_feed(text):
    root=ET.fromstring(text);out=[]
    for e in root.iter():
        if e.tag.split('}')[-1] not in ('item','entry'):continue
        fields={}
        for child in e:
            tag=child.tag.split('}')[-1]
            if tag=='link' and child.attrib.get('href'):fields['link']=child.attrib['href']
            elif tag not in fields:fields[tag]=''.join(child.itertext()).strip()
        out.append({'title':fields.get('title','')[:1000], 'url':fields.get('link',''),
                    'published_raw':fields.get('pubDate') or fields.get('published') or fields.get('updated') or fields.get('date'),
                    'text':(fields.get('description') or fields.get('summary') or fields.get('content') or '')[:12000]})
    if not out:raise ValueError('Feed contained no parseable events')
    return out

def quote_check(q,now,max_age=120,max_spread=.03):
    if not q:return 'MISSING_QUOTE'
    try:
        bid=float(q['bp']);ask=float(q['ap']);time=dt(q['t'])
        if not all(math.isfinite(x) for x in (bid,ask)) or bid<=0 or ask<=0:return 'INVALID_PRICE'
        if ask<bid:return 'CROSSED_QUOTE'
        if time is None or not 0<=(now-time).total_seconds()<=max_age:return 'STALE_OR_FUTURE_QUOTE'
        if (ask-bid)/((ask+bid)/2)>max_spread:return 'WIDE_SPREAD'
        return 'OK'
    except (KeyError,ValueError,TypeError):return 'INVALID_QUOTE'

def sessions(calendar):
    zone=ZoneInfo('America/New_York');out=[]
    for x in calendar:
        day=x['date']
        out.append({'date':day,'open':datetime.fromisoformat(day+'T'+x['open']).replace(tzinfo=zone).astimezone(UTC).isoformat(),
                    'close':datetime.fromisoformat(day+'T'+x['close']).replace(tzinfo=zone).astimezone(UTC).isoformat()})
    return sorted(out,key=lambda x:x['date'])

def validate_classification(value,text):
    allowed=['monetary_policy','inflation','employment','china_policy','company_filing','other']
    if value.get('event_type') not in allowed:raise ValueError('Unknown event type')
    impacts=value.get('impacts')
    if not isinstance(impacts,list) or len(impacts)>6:raise ValueError('Invalid impact list')
    clean=[]
    for x in impacts:
        if x.get('symbol') not in SYMBOLS or x.get('direction') not in ['up','down','uncertain']:raise ValueError('Invalid impact')
        quote=x.get('evidence_quote','')
        if not isinstance(quote,str) or not quote or quote not in text:raise ValueError('Ungrounded evidence')
        clean.append({'symbol':x['symbol'],'direction':x['direction'],'evidence_quote':quote[:1000]})
    return {'event_type':value['event_type'],'impacts':clean,'consensus':None,'surprise':None,
            'label':'MODEL_HYPOTHESIS_NOT_VERIFIED_CAUSALITY','execution_enabled':False}

def fresh_event(event,now,baseline):
    published=dt(event.get('published_raw'))
    if baseline:return False
    if published is not None:return 0<=(now-published).total_seconds()<=86400
    # Date-only official releases can support forecasts from first observation,
    # never event-time latency/surprise claims. Do not invent a publication time.
    try:
        day=datetime.fromisoformat(event.get('published_date','')).date()
        return 0<=(now.date()-day).days<=1
    except (ValueError,TypeError):return False

def make_prediction(event_id,symbol,rule,direction,entry,exit_,now):
    if dt(entry['open'])<=now+timedelta(minutes=2):return None
    return dict(event_id=event_id,symbol=symbol,rule=rule,direction=direction,
        entry_session=entry['date'],exit_session=exit_['date'],entry_open_at=entry['open'],exit_open_at=exit_['open'],
        frozen_at=now.isoformat(),execution_enabled=False,assumed_cost_per_side=.0005,
        label='HYPOTHETICAL_UNDERLYING_RETURN_NOT_BROKER_PNL')

def score(prediction,bars,spy,now):
    # Check real session-open timestamps, never the daily bar's midnight anchor.
    if dt(prediction['frozen_at'])>=dt(prediction['entry_open_at']):return None
    if now<dt(prediction['exit_open_at'])+timedelta(minutes=15):return None
    en=prediction['entry_session'];ex=prediction['exit_session']
    if any(d not in b for d in [en,ex] for b in [bars,spy]):return None
    a=float(bars[en]['o']);b=float(bars[ex]['o']);sa=float(spy[en]['o']);sb=float(spy[ex]['o'])
    if min(a,b,sa,sb)<=0:return None
    direction=prediction['direction'];net=direction*(b/a-1)-.0005*(1+b/a)
    benchmark=direction*(sb/sa-1)-.0005*(1+sb/sa)
    return dict(return_pct=100*net,matched_direction_spy_pct=100*benchmark,excess_pct=100*(net-benchmark),
        won=net>0,entry_open=a,exit_open=b,short_borrow_not_simulated=direction<0,
        label='HYPOTHETICAL_UNDERLYING_RETURN_NOT_BROKER_PNL')
