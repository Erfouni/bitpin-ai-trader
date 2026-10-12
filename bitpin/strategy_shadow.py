"""Prospective public-source archive. Never calls an account, model or network."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import zlib
from .defi_daily import atomic_text, DailyLock
from .evidence_policy import assemble_packet

MARKER = '.strategy-shadow-state'
MARKER_TEXT = 'bitpin-strategy-shadow-v1\n'
MAX_BYTES = 2*1024*1024
INPUTS = {
    'flow': Path('/var/lib/bitpin-flow-public/snapshot.json'),
    'defi': Path('/var/lib/bitpin-defi-shadow/defi-daily.json'),
    'custody_whale': Path('/var/lib/bitpin-whale-shadow/whale-shadow.json'),
    'personal_whale': Path('/var/lib/bitpin-personal-whale-shadow/personal-whale-shadow.json')}


def prepare_directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in [path]+list(path.parents)):
        raise ValueError('symlink state directory forbidden')
    path.mkdir(parents=True,exist_ok=True)
    marker = path/MARKER
    if not marker.exists():
        if any(path.iterdir()):
            raise ValueError('state directory must be empty or own marked state')
        with marker.open('x',encoding='utf-8') as out:
            out.write(MARKER_TEXT)
    if marker.is_symlink() or marker.read_text(encoding='utf-8') != MARKER_TEXT:
        raise ValueError('invalid state marker')
    for child in path.iterdir():
        if child.is_symlink():
            raise ValueError('symlink state artifact forbidden')
    return path.resolve()


def _encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


class StrategyStore:
    def __init__(self,path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError('symlink database forbidden')
        self.db = sqlite3.connect(str(path),timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA max_page_count=131072')  # 512 MiB; fail clearly, never prune receipts.
        self.db.executescript('''CREATE TABLE IF NOT EXISTS receipts (
          source TEXT NOT NULL,digest TEXT NOT NULL,first_seen REAL NOT NULL,payload BLOB NOT NULL,
          PRIMARY KEY(source,digest));
          CREATE TABLE IF NOT EXISTS events (
          time REAL NOT NULL,source TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,
          PRIMARY KEY(time,source));''')

    def close(self): self.db.close()

    def receipt_count(self):
        return self.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]

    def save(self,source,payload,now):
        if source not in INPUTS or isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now):
            raise ValueError('invalid receipt identity')
        latest = self.db.execute('SELECT MAX(time) FROM events').fetchone()[0]
        if latest is not None and now < latest:
            raise ValueError('archive clock moved backwards')
        raw = _encode(payload)
        if len(raw)>MAX_BYTES:
            raise ValueError('public snapshot exceeds limit')
        digest=hashlib.sha256(raw).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?)',(source,digest,now,zlib.compress(raw,9)))
        first=self.db.execute('SELECT first_seen FROM receipts WHERE source=? AND digest=?',(source,digest)).fetchone()[0]
        status=str(payload.get('status','ok'))[:40]
        self.db.execute('INSERT INTO events VALUES(?,?,?,?)',(now,source,digest,status))
        return {'sha256':digest,'first_observed_at':first,'archive_received_at':now,'status':status}


def read_public_snapshots():
    result={}
    for source,path in INPUTS.items():
        try:
            if path.is_symlink() or path.resolve()!=path or any(p.is_symlink() for p in path.parents):
                raise ValueError('symlink public input forbidden')
            with path.open('rb') as handle: raw=handle.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES: raise ValueError('oversized snapshot')
            payload=json.loads(raw.decode('utf-8'),parse_constant=lambda x: (_ for _ in ()).throw(ValueError('invalid constant')))
            if not isinstance(payload,dict): raise ValueError('object snapshot required')
            result[source]=payload
        except (OSError,ValueError,RecursionError):
            result[source]={'status':'input_unavailable','generated_at':time.time()}
    return result


def _numeric(metrics):
    return {k:v for k,v in metrics.items() if not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v)}


def _observations(snapshots,receipts,now):
    result=[]
    def add(source,group,symbol,fact,unit,event,received,status,metrics,max_age,receipt_age=None):
        receipt=receipts[source]
        result.append({'source':source,'group':group,'symbol':symbol,'fact_id':fact,'unit':unit,
                       'event_time':event,'received_at':received,'first_observed_at':receipt['first_observed_at'],
                       'max_age':max_age,'receipt_max_age':receipt_age or max_age,'status':status,
                       'metrics':_numeric(metrics),'sha256':receipt['sha256']})
    for source,snapshot in snapshots.items():
        generated=snapshot.get('generated_at',now)
        if source=='flow':
            for market,row in snapshot.get('markets',{}).items():
                # Short windows remain archived but cannot vote on this daily target.
                daily=(row.get('windows') or {}).get('86400') or {}
                state=row.get('trades') or {}
                status='ok' if daily.get('quality')=='ready' and state.get('status')=='ok' else daily.get('quality',state.get('status','missing'))
                base=market.split('_')[0]
                add(source,'flow',base+'_IRT','daily_aggressor_'+market,row.get('quote_unit','unknown'),
                    daily.get('as_of',generated),state.get('received_at',generated),status,
                    {k:daily.get(k) for k in ('pressure_ratio','buy_ratio','delta_quote','trade_count','coverage_seconds')},9000)
        elif source=='defi':
            for key,row in snapshot.get('sources',{}).items():
                f=row.get('features') or {}
                status='ok' if row.get('status')=='ok' and f.get('quality')=='ok' else row.get('status','missing')
                add(source,'macro','GLOBAL',key,f.get('unit','unknown'),f.get('as_of',generated),
                    row.get('received_at',generated),status,
                    {k:f.get(k) for k in ('value','change_1d_pct','change_7d_pct','change_30d_pct')},172800,9000)
        else:
            event=(snapshot.get('anchor') or {}).get('timestamp',snapshot.get('received_at',generated))
            wallets=snapshot.get('wallets') or ([{'address':snapshot.get('address','legacy'),'balance_wei':snapshot.get('balance_wei')}] if source=='personal_whale' else [])
            if not wallets:
                wallets=[{'address':'coverage'}]
            for wallet in wallets:
                unit='wei' if source=='personal_whale' else 'satoshi'
                asset='ETH_IRT' if source=='personal_whale' else 'BTC_IRT'
                add(source,'whale',asset,'native_balance:'+str(wallet.get('address')),unit,event,
                    snapshot.get('received_at',generated),snapshot.get('status','missing'),
                    {k:wallet.get(k) for k in ('balance_wei','balance_sats','change_24h_wei','change_7d_wei','change_24h_sats','change_7d_sats')},9000)
    return result


def archive_snapshots(store,snapshots,now):
    receipts={}
    with store.db:
        for source,payload in sorted(snapshots.items()):
            receipts[source]=store.save(source,payload,now)
    observations=_observations(snapshots,receipts,now)
    packets={s:assemble_packet(now,s,observations,[1/3.,1/3.,1/3.]) for s in ('BTC_IRT','ETH_IRT','SOL_IRT')}
    return {'schema_version':1,'generated_at':now,'mode':'shadow','live_enabled':False,
            'model_calls':0,'orders_created':0,'status':'ok','promotion_status':'research_only',
            'sources':receipts,'receipt_count':store.receipt_count(),'packets':packets,
            'fresh_kimi_status':'not_run','baseline_status':'uninformed_reference_only',
            'limitations':['No prospective influence has been calibrated yet.',
                           'Uniform probabilities are a reference, not a trading recommendation.',
                           'Archived snapshots are first available at the recorded archive receipt.',
                           'Wallet balances are not purchases or sales; TVL is not net inflow.']}
