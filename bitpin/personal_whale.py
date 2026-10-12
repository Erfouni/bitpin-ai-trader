"""Publicly attributed personal ETH wallet pilot; isolated from trading."""
import http.client
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from .flow_public import NoRedirect, PublicDataError, retry_seconds
from .whale_public import IPv4HTTPSHandler
from .whale_shadow import DAY, MAX_AGE, WhaleStore
from .defi_daily import DailyLock, atomic_text
from .personal_wallets import ADDRESSES, WALLETS

ADDRESS = WALLETS[0]['address']
PROVIDERS = {'publicnode': 'https://ethereum-rpc.publicnode.com', 'drpc': 'https://eth.drpc.org'}
ATTRIBUTION = dict(WALLETS[0]['attribution'])
MARKER = '.personal-whale-shadow-state'
MARKER_TEXT = 'personal-ethereum-wallet-shadow-v1\n'


def quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x(?:0|[1-9a-f][0-9a-f]{0,63})', value):
        raise ValueError('invalid RPC quantity')
    return int(value, 16)


def block(value, now, fresh=True):
    if not isinstance(value, dict): raise ValueError('missing block')
    number, timestamp = quantity(value.get('number')), quantity(value.get('timestamp'))
    digest = value.get('hash')
    if not isinstance(digest, str) or not re.fullmatch(r'0x[0-9a-f]{64}', digest):
        raise ValueError('invalid block hash')
    if not 0 < number < 100000000 or timestamp > now + 120 or (fresh and now - timestamp > 7200):
        raise ValueError('invalid or stale finalized block')
    return {'number': number, 'hash': digest, 'timestamp': timestamp}


def rpc_result(raw):
    try:
        def invalid(value): raise ValueError('nonfinite JSON')
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result: raise ValueError('duplicate JSON key')
                result[key] = item
            return result
        value = json.loads(raw.decode('utf-8'), parse_constant=invalid, object_pairs_hook=unique)
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 40 or (isinstance(item, float) and not math.isfinite(item)):
                raise ValueError('invalid JSON value')
            children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
            stack.extend((child, depth + 1) for child in children)
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or type(value.get('id')) is not int or \
                value['id'] != 1 or 'error' in value or value.get('result') is None:
            raise ValueError('invalid RPC envelope')
        return value['result']
    except (ValueError, UnicodeError, RecursionError):
        raise PublicDataError('invalid public RPC response', 3600)


class PersonalClient:
    def __init__(self, opener=None, timeout=12):
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), IPv4HTTPSHandler())
        self.timeout = timeout

    def call(self, provider, method, params):
        if provider not in PROVIDERS or not isinstance(params, list): raise ValueError('unapproved provider/params')
        allowed = method == 'eth_chainId' and params == []
        if method == 'eth_getBlockByNumber' and len(params) == 2 and params[1] is False:
            allowed = params[0] == 'finalized' or isinstance(params[0], str) and bool(
                re.fullmatch(r'0x(?:0|[1-9a-f][0-9a-f]{0,7})', params[0]))
        if method == 'eth_getBalance' and len(params) == 2 and isinstance(params[0], str) and params[0] in ADDRESSES:
            allowed = isinstance(params[1], str) and bool(re.fullmatch(r'0x(?:0|[1-9a-f][0-9a-f]{0,7})', params[1]))
        if not allowed: raise ValueError('unapproved read-only RPC request')
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode('ascii')
        request = urllib.request.Request(PROVIDERS[provider], data=raw, method='POST',
            headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'BitpinPersonalWhaleShadow/1.0'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = response.read(1024 * 1024 + 1)
                if len(result) > 1024 * 1024: raise PublicDataError('RPC response too large', 3600)
        except urllib.error.HTTPError as exc:
            code = exc.code
            delay = retry_seconds(exc.headers.get('Retry-After')) if code in (429, 503) else 3600
            exc.close()
            raise PublicDataError('public HTTP %d' % code, delay, code)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise PublicDataError('public network error: ' + type(exc).__name__, 3600)
        return rpc_result(result)


def prepare_directory(value):
    original = Path(value).absolute()
    markers = ('LIVE_CONFIRMED', 'bitpin_token.json', '.flow-shadow-state', '.defi-shadow-state', '.whale-shadow-state')
    for parent in (original,) + tuple(original.parents):
        # Python 3.7 does not expose Path.is_junction; Windows reparse aliases
        # must also be excluded before any state creation.
        reparse = parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400
        if parent.is_symlink() or reparse or any((parent / name).exists() for name in markers):
            raise ValueError('choose isolated nonsymlink state')
    directory = original.resolve()
    if any((parent / name).exists() for parent in (directory,) + tuple(directory.parents) for name in markers):
        raise ValueError('choose isolated resolved state')
    normalized = directory.as_posix().lower() + '/'
    blocked = ('/etc/bitpin-bot/', '/opt/bitpin-bot/', '/var/lib/bitpin-bot/', '/var/lib/bitpin-bot-paper/',
               '/var/lib/bitpin-flow-public/', '/var/lib/bitpin-defi-shadow/', '/var/lib/bitpin-whale-shadow/')
    if any(part in normalized for part in blocked): raise ValueError('choose isolated state')
    marker = directory / MARKER
    if directory.exists() and not marker.is_file() and any(directory.iterdir()):
        raise ValueError('choose an empty directory')
    directory.mkdir(parents=True, exist_ok=True)
    if marker.is_symlink(): raise ValueError('invalid marker')
    if not marker.exists(): marker.write_text(MARKER_TEXT, encoding='utf-8')
    if marker.read_text(encoding='utf-8') != MARKER_TEXT: raise ValueError('invalid marker')
    for name in ('personal.sqlite3', 'personal.sqlite3-wal', 'personal.sqlite3-shm', '.collector.lock',
                 'personal-whale-shadow.json', 'report.html'):
        if (directory / name).is_symlink(): raise ValueError('state symlinks are not allowed')
    return directory


class PersonalStore(WhaleStore):
    @staticmethod
    def wallet_balance(record, address):
        balances = record.get('wallet_balances', {})
        value = balances.get(address) if isinstance(balances, dict) else None
        # Only the original Vitalik scalar has a legitimate legacy meaning.
        if value is None and address == ADDRESS:
            value = record.get('balance_wei')
        return value if type(value) is int and value >= 0 else None

    def wallet_reference(self, received, interval, epoch, address):
        target = received - interval
        rows = self.db.execute('SELECT record FROM observations WHERE received BETWEEN ? AND ? '
                               'ORDER BY abs(received-?) ASC, id DESC',
                               (target - 3600, target + 3600, target))
        for row in rows:
            record = json.loads(row[0])
            if record['status'] == 'ok' and record['epoch'] == epoch and \
                    self.wallet_balance(record, address) is not None:
                return record
        return None

    def snapshot(self, now):
        record = self.latest() or {'status': 'missing', 'received_at': None, 'sources': {}, 'epoch': self.epoch()}
        received, status = record['received_at'], record['status']
        if received is not None and not 0 <= now - received <= MAX_AGE: status = 'stale'
        usable = status == 'ok'
        result = {'schema_version': 1, 'mode': 'shadow', 'live_enabled': False, 'orders_created': 0,
                  'model_calls': 0, 'generated_at': now, 'received_at': received, 'status': status,
                  'receipt_age_seconds': now - received if received is not None else None,
                  'sources': record['sources'], 'request_count': record.get('request_count'),
                  'address': ADDRESS, 'asset': 'ETH', 'chain_id': 1, 'attribution': dict(ATTRIBUTION),
                  'current_ownership_verified': False, 'complete_portfolio': False, 'complete_daily_flow': False,
                  'chain_epoch': record['epoch'], 'balance_wei': record.get('balance_wei') if usable else None,
                  'anchor': record.get('anchor') if usable else None,
                  'interpretation': 'Three publicly associated Ethereum addresses, not a representative whale cohort. '
                  'Current ownership unverified. Native ETH only; tokens, staking and DeFi excluded. '
                  'Balance changes include transfers, fees and funding; not purchases, sales or gross flows. '
                  'No established predictive edge; no effect on live decisions.'}
        wallets = []
        for wallet in WALLETS:
            row = dict(wallet)
            row['attribution'] = dict(wallet['attribution'])
            balance = self.wallet_balance(record, wallet['address']) if usable else None
            row.update(balance_wei=balance, asset='ETH', chain_id=1,
                       status='ok' if balance is not None else 'missing' if usable else status,
                       current_ownership_verified=False, personal_funds_verified=False,
                       complete_portfolio=False)
            for name, interval in (('24h', DAY), ('7d', 7 * DAY)):
                reference = self.wallet_reference(received, interval, record['epoch'], wallet['address']) \
                    if balance is not None else None
                prior = self.wallet_balance(reference, wallet['address']) if reference else None
                row['change_' + name + '_wei'] = balance - prior if prior is not None else None
                row['reference_' + name + '_balance_wei'] = prior
                row['reference_' + name + '_received_at'] = reference['received_at'] if reference else None
                row['reference_' + name + '_anchor'] = reference.get('anchor') if reference else None
                row['reference_' + name + '_elapsed_seconds'] = received - reference['received_at'] if reference else None
                row['reference_' + name + '_block_elapsed_seconds'] = \
                    record['anchor']['timestamp'] - reference['anchor']['timestamp'] if reference else None
            wallets.append(row)
        result['wallets'] = wallets
        # Preserve all original scalar fields for existing consumers.
        result['balance_wei'] = wallets[0]['balance_wei']
        for name in ('24h', '7d'):
            for field in ('change_' + name + '_wei', 'reference_' + name + '_elapsed_seconds',
                          'reference_' + name + '_block_elapsed_seconds'):
                result[field] = wallets[0][field]
        return result


class PersonalCollector:
    def __init__(self, store, client=None, clock=time.time, sleeper=time.sleep):
        self.store, self.client = store, client or PersonalClient()
        self.clock, self.sleeper = clock, sleeper
        self.requests, self.last_provider = 0, None

    def call(self, provider, method, params):
        if self.requests >= 20: raise PublicDataError('request budget exhausted', 3600)
        if self.requests: self.sleeper(2)
        self.requests += 1
        self.last_provider = provider
        return self.client.call(provider, method, params)

    def failed(self, provider, error, sources):
        sources[provider] = {'status': 'error', 'reason': type(error).__name__}
        delay = max(3600, min(86400, getattr(error, 'retry_after', 3600)))
        self.store.cooldown(provider, self.clock() + delay)

    def collect(self, publish=None):
        previous = self.store.latest(successful=True)
        record = {'status': 'collecting', 'received_at': self.clock(), 'epoch': self.store.epoch(), 'sources': {}}
        self.store.save(record)
        if publish: publish(self.store.snapshot(self.clock()))
        heads, sources = {}, record['sources']
        for provider in PROVIDERS:
            if self.store.cooldown(provider) > self.clock():
                sources[provider] = {'status': 'cooldown'}
                continue
            try:
                if quantity(self.call(provider, 'eth_chainId', [])) != 1: raise ValueError('wrong chain')
                heads[provider] = block(self.call(provider, 'eth_getBlockByNumber', ['finalized', False]), self.clock())
                sources[provider] = {'status': 'ok', 'finalized_height': heads[provider]['number']}
            except (PublicDataError, ValueError) as error:
                self.failed(provider, error, sources)
        record['status'] = 'error'
        if len(heads) == 2:
            try:
                if abs(heads['publicnode']['number'] - heads['drpc']['number']) > 128:
                    raise ValueError('finalized heads too far apart')
                height = min(row['number'] for row in heads.values())
                rollback = previous and previous.get('anchor') and height < previous['anchor']['number']
                anchors, balances, old = {}, {}, {}
                for provider in PROVIDERS:
                    anchor = block(self.call(provider, 'eth_getBlockByNumber', [hex(height), False]), self.clock())
                    if anchor['number'] != height: raise ValueError('wrong anchor height')
                    if heads[provider]['number'] == height and anchor != heads[provider]:
                        raise ValueError('unstable finalized head')
                    anchors[provider] = anchor
                    balances[provider] = {wallet['address']: quantity(self.call(provider, 'eth_getBalance',
                        [wallet['address'], hex(height)])) for wallet in WALLETS}
                    if previous and previous.get('anchor') and not rollback:
                        old_height = previous['anchor']['number']
                        old[provider] = block(self.call(provider, 'eth_getBlockByNumber', [hex(old_height), False]), self.clock(), fresh=False)
                        if old[provider]['number'] != old_height: raise ValueError('wrong previous height')
                if anchors['publicnode'] != anchors['drpc'] or balances['publicnode'] != balances['drpc'] or \
                        (old and old['publicnode'] != old['drpc']):
                    record['status'] = 'disputed'
                else:
                    anchor = anchors['publicnode']
                    reorg = previous and previous.get('anchor') and (height < previous['anchor']['number'] or
                        old['publicnode']['hash'] != previous['anchor']['hash'] or
                        old['publicnode']['timestamp'] != previous['anchor']['timestamp'])
                    for provider in PROVIDERS:
                        final = block(self.call(provider, 'eth_getBlockByNumber', [hex(height), False]), self.clock())
                        if final != anchor: raise ValueError('anchor changed during collection')
                    record.update(status='reorg' if reorg else 'ok', anchor=anchor,
                                  balance_wei=balances['publicnode'][ADDRESS], wallet_balances=balances['publicnode'])
                    if reorg: record['epoch'] = self.store.epoch(advance=True)
            except (PublicDataError, ValueError) as error:
                record['status'] = 'error'
                self.failed(self.last_provider, error, sources)
        record.update(received_at=self.clock(), request_count=self.requests)
        self.store.save(record)
        return self.store.snapshot(self.clock())


def write_snapshot(snapshot, directory):
    atomic_text(directory, 'personal-whale-shadow.json', json.dumps(snapshot, indent=2, allow_nan=False) + '\n')
