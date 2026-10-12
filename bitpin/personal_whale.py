"""Publicly attributed personal ETH wallet pilot; isolated from trading."""
import http.client
import json
import math
from pathlib import Path
import re
import socket
import ssl
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
RPC_METHODS = frozenset(('eth_chainId', 'eth_getBlockByNumber', 'eth_getBalance'))
RPC_FAILURE_KINDS = frozenset(('timeout', 'connection_reset', 'connection_closed', 'tls_certificate',
    'tls_error', 'network_error', 'http_rate_limit', 'http_error', 'rpc_error', 'invalid_response',
    'response_too_large', 'request_budget', 'validation_error', 'public_error'))


class RPCFailure(PublicDataError):
    """Only fixed categories and bounded identifiers survive a public RPC failure."""
    def __init__(self, kind, retry_after=3600, http_status=None, rpc_code=None, method=None):
        self.kind = kind if isinstance(kind, str) and kind in RPC_FAILURE_KINDS else 'public_error'
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.rpc_code = rpc_code if type(rpc_code) is int and -(2 ** 31) <= rpc_code < 2 ** 31 else None
        self.method = method if isinstance(method, str) and method in RPC_METHODS else None
        if type(retry_after) not in (int, float) or (type(retry_after) is float and not math.isfinite(retry_after)):
            retry_after = 3600
        super().__init__('public RPC ' + self.kind, max(0, min(86400, retry_after)), self.http_status)


def _network_kind(error):
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, ssl.SSLCertVerificationError): return 'tls_certificate'
    if isinstance(cause, (http.client.RemoteDisconnected, http.client.IncompleteRead, ssl.SSLEOFError)):
        return 'connection_closed'
    if isinstance(cause, ssl.SSLError): return 'tls_error'
    if isinstance(cause, (TimeoutError, socket.timeout)): return 'timeout'
    if isinstance(cause, ConnectionResetError): return 'connection_reset'
    return 'network_error'


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


def rpc_result(raw, method=None):
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
                value['id'] != 1:
            raise ValueError('invalid RPC envelope')
        if 'error' in value:
            error = value['error']
            if ('result' in value or not isinstance(error, dict) or type(error.get('code')) is not int or
                    not -(2 ** 31) <= error['code'] < 2 ** 31 or not isinstance(error.get('message'), str)):
                raise ValueError('invalid RPC error')
            raise RPCFailure('rpc_error', rpc_code=error['code'], method=method)
        if value.get('result') is None: raise ValueError('missing RPC result')
        return value['result']
    except (ValueError, UnicodeError, RecursionError):
        raise RPCFailure('invalid_response', method=method)


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
                if len(result) > 1024 * 1024: raise RPCFailure('response_too_large', method=method)
        except urllib.error.HTTPError as exc:
            code = exc.code
            delay = retry_seconds(exc.headers.get('Retry-After')) if code in (429, 503) else 3600
            exc.close()
            raise RPCFailure('http_rate_limit' if code == 429 else 'http_error', delay,
                             http_status=code, method=method)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise RPCFailure(_network_kind(exc), method=method)
        except RPCFailure:
            raise
        except PublicDataError as exc:
            # Redirect refusals and legacy transport handlers must not preserve prose.
            raise RPCFailure('public_error', exc.retry_after, http_status=exc.status, method=method)
        return rpc_result(result, method=method)


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
        self.requests, self.last_provider, self.last_method = 0, None, None
        self._rate_limit_retried = False

    def call(self, provider, method, params):
        self.last_provider, self.last_method = provider, method
        delay = 2 if self.requests else 0
        while True:
            if self.requests >= 20: raise RPCFailure('request_budget', method=method)
            if delay: self.sleeper(delay)
            self.requests += 1
            try:
                return self.client.call(provider, method, params)
            except RPCFailure as error:
                if (error.kind != 'http_rate_limit' or error.http_status != 429 or
                        self._rate_limit_retried or error.retry_after > 60 or self.requests >= 20):
                    raise
                # One repeat shared by both gateways; it consumes the same wire budget.
                self._rate_limit_retried = True
                delay = max(2, error.retry_after)

    def failed(self, provider, error, sources):
        delay = max(3600, min(86400, getattr(error, 'retry_after', 3600)))
        retry_at = self.clock() + delay
        classified = error if isinstance(error, RPCFailure) else RPCFailure(
            'validation_error' if isinstance(error, ValueError) else 'public_error',
            http_status=getattr(error, 'status', None), method=self.last_method)
        row = {'status': 'error', 'reason': 'RPCFailure' if isinstance(error, RPCFailure) else
               'ValueError' if isinstance(error, ValueError) else 'PublicDataError',
               'error_kind': classified.kind, 'retry_at': retry_at}
        method = classified.method or (self.last_method if self.last_method in RPC_METHODS else None)
        if method is not None: row['method'] = method
        if classified.http_status is not None: row['http_status'] = classified.http_status
        if classified.rpc_code is not None: row['rpc_code'] = classified.rpc_code
        sources[provider] = row
        self.store.cooldown(provider, retry_at)

    def collect(self, publish=None):
        previous = self.store.latest(successful=True)
        record = {'status': 'collecting', 'received_at': self.clock(), 'epoch': self.store.epoch(), 'sources': {}}
        self.store.save(record)
        if publish: publish(self.store.snapshot(self.clock()))
        heads, sources = {}, record['sources']
        for provider in PROVIDERS:
            retry_at = self.store.cooldown(provider)
            if retry_at > self.clock():
                sources[provider] = {'status': 'cooldown', 'retry_at': retry_at}
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
