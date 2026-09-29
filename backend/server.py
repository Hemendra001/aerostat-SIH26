"""AeroStat single-process collection service. Python 3.11+."""
import concurrent.futures
import hmac
import importlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
ROUTES = ['DEL-BOM', 'DEL-BLR', 'BLR-BOM', 'DEL-CCU', 'BOM-HYD', 'DEL-MAA']
HORIZONS = [1, 7, 15, 30, 45]
DB = os.getenv('AEROSTAT_DB', 'aerostat.sqlite3')
TOKEN = os.getenv('AEROSTAT_TOKEN', '')
ORIGIN = os.getenv('AEROSTAT_ORIGIN', 'https://aerostat-omega.hemendra-kumar-3007.chatgpt.site')
MODE = os.getenv('AEROSTAT_MODE', 'demo')
ADAPTER = os.getenv('AEROSTAT_ADAPTER', '')
LOCK = threading.Lock()
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1)

def now():
    return datetime.now(IST)

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def initialize():
    with db() as c:
        c.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started TEXT, mode TEXT, status TEXT, count INTEGER, detail TEXT, scheduled_day TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS fares (id TEXT PRIMARY KEY, day TEXT, mode TEXT, run_id TEXT, payload TEXT);
        CREATE INDEX IF NOT EXISTS fares_day ON fares(day,mode);''')
        c.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (json.dumps({'time':'06:00','enabled':False,'timezone':'Asia/Kolkata','routes':ROUTES,'horizons':HORIZONS}),))
        c.execute("UPDATE runs SET status='interrupted',detail='Service restarted before run completed' WHERE status='running'")

def config():
    with db() as c:
        return json.loads(c.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])

def validate_plan(p):
    if not isinstance(p,dict) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(p.get('time',''))):
        raise ValueError('Use a daily time in HH:MM format.')
    if p.get('routes') != ROUTES or p.get('horizons') != HORIZONS:
        raise ValueError('Use the configured six-route, five-horizon basket.')
    if p.get('timezone') != 'Asia/Kolkata':
        raise ValueError('Timezone must be Asia/Kolkata.')
    return {**p,'enabled':bool(p.get('enabled',False))}

def status():
    with db() as c:
        runs=[dict(r) for r in c.execute('SELECT started,mode,status,count,detail FROM runs ORDER BY started DESC LIMIT 20')]
    return {'mode':MODE,'running':LOCK.locked(),'schedule':config(),'runs':runs}

def valid_fare(raw,route,horizon,day,run_id):
    if not isinstance(raw,dict): raise ValueError('Adapter result must be an object.')
    fare=raw.get('fare')
    if type(fare) not in (int,float) or not math.isfinite(fare) or fare<=0: raise ValueError('Invalid fare.')
    for key in ('airline','flight','source'):
        if not isinstance(raw.get(key),str) or not raw[key].strip(): raise ValueError('Missing '+key)
    expected=(datetime.fromisoformat(day)+timedelta(days=horizon)).date().isoformat()
    if raw.get('departureDate')!=expected: raise ValueError('Departure date mismatch.')
    if raw.get('currency')!='INR' or raw.get('cabin')!='economy' or raw.get('adults')!=1 or raw.get('stops')!=0 or raw.get('includesTaxes') is not True: raise ValueError('Fare is outside the comparable basket.')
    stamp=datetime.fromisoformat(raw['observedAt'].replace('Z','+00:00'))
    if stamp.tzinfo is None or stamp.astimezone(IST).date().isoformat()!=day or stamp>now()+timedelta(minutes=5): raise ValueError('Invalid observation time.')
    return {k:raw[k] for k in ('airline','flight','source','fare','departureDate','observedAt')} | {'id':str(uuid.uuid4()),'route':route,'horizon':horizon}

def demo_collect(route,departure_date):
    base=dict(zip(ROUTES,[5000,6500,4200,5400,3800,6200]))[route]
    horizon=(datetime.fromisoformat(departure_date).date()-now().date()).days
    multiplier=dict(zip(HORIZONS,[1.43,1.09,.96,.84,.88]))[horizon]
    return [{'airline':a,'flight':f,'source':a,'fare':round(base*(multiplier+i*.045)), 'departureDate':departure_date,'observedAt':now().isoformat(),'currency':'INR','cabin':'economy','adults':1,'stops':0,'includesTaxes':True} for i,(a,f) in enumerate([('IndiGo','DEMO 01'),('Air India','DEMO 02')])]

def collect(run_id,day,p):
    count=0; errors=[]; successful=0
    try:
        adapter=demo_collect if MODE=='demo' else importlib.import_module(ADAPTER).collect
        for route in p['routes']:
            for h in p['horizons']:
                if now().date().isoformat()!=day:
                    errors.append('Run crossed the observation-day boundary; remaining searches skipped')
                    raise RuntimeError('Day boundary')
                dep=(datetime.fromisoformat(day)+timedelta(days=h)).date().isoformat()
                try:
                    rows=list(adapter(route,dep)); seen=set(); clean=[]
                    if not rows: raise ValueError('No available fares')
                    for raw in rows:
                        o=valid_fare(raw,route,h,day,run_id)
                        key=(o['source'],o['flight'],o['departureDate'],o['fare'])
                        if key not in seen: clean.append(o);seen.add(key)
                    with db() as c:
                        for o in clean: c.execute('INSERT INTO fares VALUES (?,?,?,?,?)',(o['id'],day,MODE,run_id,json.dumps(o)))
                    count+=len(clean);successful+=1
                except Exception:
                    # Avoid exposing provider URLs, tokens or raw provider exception text.
                    errors.append(route+' T+'+str(h)+': adapter failed or returned invalid fares')
                if MODE=='live': time.sleep(2)
    except Exception:
        errors.append('Collection stopped; verify adapter configuration and service logs')
    finally:
        result='succeeded' if successful==30 else 'partial' if count else 'failed'
        detail=f'{successful}/30 searches completed. '+ ('; '.join(errors[:3]) if errors else 'All fare records validated.')
        with db() as c:c.execute('UPDATE runs SET status=?,count=?,detail=? WHERE id=?',(result,count,detail,run_id))
        LOCK.release()

def start_run(p,scheduled_day=None):
    if not LOCK.acquire(blocking=False): raise ValueError('A collection is already running.')
    run_id=str(uuid.uuid4()); day=now().date().isoformat()
    try:
        with db() as c:c.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',(run_id,now().isoformat(),MODE,'running',0,'Collection in progress',scheduled_day))
        EXECUTOR.submit(collect,run_id,day,p)
    except Exception:
        LOCK.release();raise

def scheduler():
    while True:
        try:
            p=config();current=now();day=current.date().isoformat()
            if p['enabled'] and current.strftime('%H:%M')>=p['time']:
                with db() as c: exists=c.execute('SELECT 1 FROM runs WHERE scheduled_day=?',(day,)).fetchone()
                if not exists and not LOCK.locked(): start_run(p,day)
        except Exception: pass
        time.sleep(20)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,code,data):
        payload=json.dumps(data).encode();self.send_response(code)
        self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
        self.send_header('Access-Control-Allow-Origin',ORIGIN);self.send_header('Vary','Origin')
        self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type');self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
        self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def do_OPTIONS(self): self.reply(200,{})
    def authorized(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+TOKEN):
            self.reply(401,{'error':'Invalid access token.'});return False
        if self.headers.get('Origin') not in (None,ORIGIN):
            self.reply(403,{'error':'Origin is not allowed.'});return False
        return True
    def do_GET(self):
        if not self.authorized():return
        u=urlparse(self.path)
        if u.path=='/status':return self.reply(200,status())
        if u.path=='/observations':
            day=parse_qs(u.query).get('observation_date',[now().date().isoformat()])[0]
            with db() as c:
                # Use a single completed snapshot, never blend old and new runs.
                r=c.execute("SELECT id FROM runs WHERE substr(started,1,10)=? AND mode=? AND status IN ('succeeded','partial') ORDER BY started DESC LIMIT 1",(day,MODE)).fetchone()
                rows=[json.loads(v[0]) for v in c.execute('SELECT payload FROM fares WHERE run_id=?',(r[0],))] if r else []
            return self.reply(200,{'mode':MODE,'observations':rows})
        self.reply(404,{'error':'Unknown endpoint.'})
    def do_POST(self):
        if not self.authorized():return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384: raise ValueError('Invalid request size.')
            p=validate_plan(json.loads(self.rfile.read(length)))
            if self.path=='/schedule':
                with db() as c:c.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(p),))
            elif self.path=='/run':start_run(p)
            else:return self.reply(404,{'error':'Unknown endpoint.'})
            self.reply(200,status())
        except (ValueError,sqlite3.IntegrityError):self.reply(400,{'error':'Invalid plan or a collection is already running.'})

if __name__=='__main__':
    if len(TOKEN)<24:raise SystemExit('Set AEROSTAT_TOKEN to a random value of at least 24 characters.')
    if MODE not in ('demo','live'):raise SystemExit('AEROSTAT_MODE must be demo or live.')
    if MODE=='live' and not ADAPTER:raise SystemExit('Live mode needs AEROSTAT_ADAPTER (Python module exporting collect).')
    initialize();threading.Thread(target=scheduler,daemon=True).start()
    print('AeroStat collector starting in '+MODE+' mode. Daily schedule is paused until enabled.')
    ThreadingHTTPServer((os.getenv('AEROSTAT_HOST','127.0.0.1'),int(os.getenv('PORT','8000'))),Handler).serve_forever()
