"""Opt-in native wallet observations for the existing daily decision context.

Reads public collector files only. No network, account, model or trading calls.
Collector mode remains shadow; inclusion in a model context is a separate action.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import stat

from .personal_wallets import WALLETS as PERSONAL_WALLETS
from .whale_public import MAX_MONEY, PROVENANCE, WALLETS as CUSTODY_WALLETS

DAY = 86400
MAX_AGE = 9000
MAX_ANCHOR_AGE = 7200
MAX_BYTES = 512 * 1024
MAX_WEI = 2 ** 256 - 1
INTERPRETATION = (
    'wallet_observations contains supporting daily native-balance facts, not a calibrated trading edge. '
    'Public ETH address associations do not verify current ownership or personal capital; Arthur Hayes '
    'personal versus fund capital is ambiguous. The BTC addresses are a historical Bitfinex custody '
    'sample, not independent investors or complete exchange reserves. Native ETH excludes tokens, '
    'staking and DeFi; BTC balances include confirmed transactions without absolute finality. '
    'Balance changes include transfers, fees and funding, not purchases, sales, exchange net flows '
    'or identified smart money. A positive balance change has no established bullish direction. '
    '24h/7d changes use receipt intervals with up to one hour tolerance; they are not complete daily '
    'transaction flows. Missing, stale, disputed or invalid values mean no evidence, never zero. '
    'Use only the matching asset and exact native units, window, block and receipt when discussing '
    'supporting or conflicting daily evidence. Do not sum custody and personal balances or treat '
    'individual wallets as independent confirmations. Do not create a new setup/base-rate row, '
    'add a probability bonus beyond the existing P RULE allowance, trigger a decision from a wallet '
    'alone, add model calls, or bypass existing buy, confirmation, fee, stop and position rules. '
    'Kimi remains the allocator; collector shadow mode does not describe context-consumption mode.'
)


def snapshot_path_valid(value, personal=True):
    """Only explicit absolute public-snapshot paths, without traversal or account-state aliases."""
    if not isinstance(value, str) or '\x00' in value or not os.path.isabs(value):
        return False
    if value.startswith(('\\\\', '//')) or '..' in re.split(r'[/\\]+', value):
        return False
    path = Path(value)
    normalized = path.as_posix().lower().replace('\\', '/') + '/'
    blocked = ('/etc/bitpin-bot/', '/var/lib/bitpin-bot/', '/var/lib/bitpin-bot-paper/',
               '/root/bitpin-tools/')
    return (path.name == ('personal-whale-shadow.json' if personal else 'whale-shadow.json')
            and not any(part in normalized for part in blocked))


def _read(path, limit):
    """Do not follow any ancestor, symlink, Windows reparse alias, device or pipe."""
    absolute = os.path.abspath(str(path))
    parent = fd = None
    try:
        if os.open in getattr(os, 'supports_dir_fd', set()) and hasattr(os, 'O_NOFOLLOW'):
            parent = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
            components = [p for p in absolute.split(os.path.sep) if p]
            for component in components[:-1]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        else:
            def checked_components():
                component = absolute
                while component:
                    info = os.lstat(component)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise ValueError('path alias')
                    ancestor = os.path.dirname(component)
                    if ancestor == component:
                        break
                    component = ancestor
            checked_components()
            before = os.lstat(absolute)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError('not a bounded regular file')
            fd = os.open(absolute, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0))
            checked_components()
            opened = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError('file changed during open')
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('not a bounded regular file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('oversized file')
        return raw
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def _json_number(value, integer=False):
    if len(value) > 80:
        raise ValueError('unbounded JSON number')
    result = int(value) if integer else float(value)
    if not integer and not math.isfinite(result):
        raise ValueError('nonfinite JSON')
    return result


def _json(raw):
    doc = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                     parse_int=lambda s: _json_number(s, True), parse_float=_json_number,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    stack = [(doc, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 40:
            raise ValueError('unbounded JSON depth')
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        stack.extend((child, depth + 1) for child in children)
    if not isinstance(doc, dict):
        raise ValueError('snapshot object required')
    return doc


def _number(value):
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value < 10 ** 12:
        raise ValueError('invalid timestamp or interval')
    return value


def _integer(value, maximum, signed=False):
    if type(value) is not int or abs(value) > maximum or (not signed and value < 0):
        raise ValueError('invalid native integer')
    return value


def _metadata(value, expected):
    # JSON booleans and numeric zero compare equal in Python; provenance must keep its declared types.
    return (isinstance(value, dict) and set(value) == set(expected)
            and all(type(value[key]) is type(expected[key]) and value[key] == expected[key]
                    for key in expected))


def _fresh(value, now, maximum):
    age = _number(now) - _number(value)
    if not 0 <= age <= maximum:
        raise ValueError('stale or future timestamp')
    return age


def _anchor(raw, personal, receipt, now):
    if not isinstance(raw, dict):
        raise ValueError('missing anchor')
    height_key = 'number' if personal else 'height'
    height = _integer(raw[height_key], 99999999 if personal else 9999999)
    pattern = r'0x[0-9a-f]{64}' if personal else r'[0-9a-f]{64}'
    if not height or not isinstance(raw['hash'], str) or not re.fullmatch(pattern, raw['hash']):
        raise ValueError('invalid anchor identity')
    out = {height_key: height, 'hash': raw['hash']}
    if personal:
        timestamp = _integer(raw['timestamp'], 2 ** 40 - 1)
        _fresh(timestamp, receipt, MAX_ANCHOR_AGE)
        _fresh(timestamp, now, MAX_ANCHOR_AGE)
        out['timestamp'] = timestamp
    return out


def _envelope(doc, personal, now):
    if (type(doc['schema_version']) is not int or doc['schema_version'] != 1 or doc['mode'] != 'shadow'
            or doc['live_enabled'] is not False or type(doc['orders_created']) is not int
            or doc['orders_created'] != 0 or type(doc['model_calls']) is not int or doc['model_calls'] != 0
            or doc['status'] != 'ok' or doc['complete_daily_flow'] is not False):
        raise ValueError('collector envelope mismatch')
    _integer(doc['chain_epoch'], 2 ** 31 - 1)
    _integer(doc['request_count'], 20 if personal else 64)
    generated, received = _number(doc['generated_at']), _number(doc['received_at'])
    _fresh(generated, now, MAX_AGE)
    age = _fresh(received, now, MAX_AGE)
    if received > generated or type(doc['receipt_age_seconds']) not in (float, int) or \
            doc['receipt_age_seconds'] != generated - received:
        raise ValueError('inconsistent collector clock')
    anchor = _anchor(doc['anchor'], personal, received, now)
    providers = ('publicnode', 'flashbots') if personal else ('blockstream', 'mempool')
    key = 'finalized_height' if personal else 'tip_height'
    if not isinstance(doc['sources'], dict) or set(doc['sources']) != set(providers):
        raise ValueError('provider identity mismatch')
    heights = []
    for provider in providers:
        source = doc['sources'][provider]
        if not isinstance(source, dict) or source['status'] != 'ok':
            raise ValueError('provider unavailable')
        heights.append(_integer(source[key], 99999999 if personal else 9999999))
    if (max(heights) - min(heights) > (128 if personal else 2)
            or anchor['number' if personal else 'height'] != min(heights) - (0 if personal else 5)):
        raise ValueError('provider/anchor disagreement')
    return {'generated_at': generated, 'received_at': received, 'receipt_age_s': round(age, 1),
            'anchor': anchor, 'providers': list(providers)}


def _change(row, name, interval, balance, personal, received, anchor):
    unit, maximum = ('wei', MAX_WEI) if personal else ('sats', MAX_MONEY)
    key = 'change_' + name + '_' + unit
    value = row.get(key)
    if value is None:
        return None, None
    try:
        value = _integer(value, maximum, signed=True)
        elapsed = _number(row['reference_' + name + '_elapsed_seconds'])
        if not interval - 3600 <= elapsed <= interval + 3600:
            raise ValueError('reference outside daily interval')
        prior = _integer(balance - value, maximum)
        if personal:
            prior_balance = _integer(row['reference_' + name + '_balance_wei'], maximum)
            reference_received = _number(row['reference_' + name + '_received_at'])
            previous = _anchor(row['reference_' + name + '_anchor'], True,
                               reference_received, reference_received)
            block_elapsed = _number(row['reference_' + name + '_block_elapsed_seconds'])
            if (prior_balance != prior or received - reference_received != elapsed
                    or previous['number'] >= anchor['number'] or previous['hash'] == anchor['hash']
                    or anchor['timestamp'] - previous['timestamp'] != block_elapsed):
                raise ValueError('inconsistent reference')
        return value, elapsed
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, None


def _wallet(row, catalog, personal, received, anchor):
    if not isinstance(row, dict) or any(row.get(key) != catalog[key] for key in ('address', 'label')):
        raise ValueError('wallet identity mismatch')
    if personal:
        if (row.get('person') != catalog['person'] or row.get('kind') != catalog['kind']
                or not _metadata(row.get('attribution'), catalog['attribution']) or row.get('status') != 'ok'
                or row.get('asset') != 'ETH' or type(row.get('chain_id')) is not int or row['chain_id'] != 1
                or row.get('current_ownership_verified') is not False
                or row.get('personal_funds_verified') is not False or row.get('complete_portfolio') is not False):
            raise ValueError('wallet scope or association mismatch')
    unit, asset, maximum = ('wei', 'ETH', MAX_WEI) if personal else ('sats', 'BTC', MAX_MONEY)
    balance = _integer(row['balance_' + unit], maximum)
    out = {'status': 'ok', 'address': catalog['address'], 'label': catalog['label'], 'asset': asset,
           'balance_' + unit: balance, 'current_ownership_verified': False,
           'personal_funds_verified': False, 'complete_portfolio': False}
    if personal:
        attribution = catalog['attribution']
        out.update(person=catalog['person'], association_strength=attribution['association_strength'],
                   address_source=attribution['address_source'])
    else:
        out['kind'] = 'historical_custody_sample'
        out['address_source'] = PROVENANCE['url']
    for name, interval in (('24h', DAY), ('7d', 7 * DAY)):
        change, elapsed = _change(row, name, interval, balance, personal, received, anchor)
        out['change_' + name + '_' + unit] = change
        out['reference_' + name + '_elapsed_seconds'] = elapsed
    return out


def _section(path, personal, now):
    if path is None:
        return {'status': 'disabled', 'available_wallets': 0}
    unavailable = {'status': 'unavailable', 'available_wallets': 0}
    try:
        if not snapshot_path_valid(path, personal):
            return unavailable
        target = Path(path)
        marker = '.personal-whale-shadow-state' if personal else '.whale-shadow-state'
        text = b'personal-ethereum-wallet-shadow-v1' if personal else b'bitcoin-wallet-shadow-v1'
        if _read(target.parent / marker, 64) not in (text + b'\n', text + b'\r\n'):
            return unavailable
        doc = _json(_read(target, MAX_BYTES))
        section = _envelope(doc, personal, now)
        if personal:
            catalog = PERSONAL_WALLETS
            if (doc['address'] != catalog[0]['address'] or doc['asset'] != 'ETH'
                    or type(doc['chain_id']) is not int or doc['chain_id'] != 1
                    or not _metadata(doc['attribution'], catalog[0]['attribution'])
                    or doc['current_ownership_verified'] is not False or doc['complete_portfolio'] is not False):
                return unavailable
        else:
            catalog = CUSTODY_WALLETS
            if (not _metadata(doc['provenance'], PROVENANCE) or type(doc['minimum_transaction_confirmations']) is not int
                    or doc['minimum_transaction_confirmations'] != 6
                    or type(doc['large_movement_threshold_sats']) is not int
                    or doc['large_movement_threshold_sats'] != 50000000000):
                return unavailable
        raw_wallets = doc['wallets']
        if not isinstance(raw_wallets, list) or len(raw_wallets) > len(catalog):
            return unavailable
        by_address = {}
        for row in raw_wallets:
            if not isinstance(row, dict) or not isinstance(row.get('address'), str):
                continue
            if row['address'] in by_address:
                return unavailable
            by_address[row['address']] = row
        if personal:
            primary = by_address.get(catalog[0]['address'], {})
            if doc['balance_wei'] != primary.get('balance_wei') or type(doc['balance_wei']) is bool:
                return unavailable
        wallets = []
        for entry in catalog:
            try:
                wallets.append(_wallet(by_address[entry['address']], entry, personal,
                                       section['received_at'], section['anchor']))
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        if not wallets:
            return unavailable
        section.update(status='ok' if len(wallets) == len(catalog) else 'partial',
                       available_wallets=len(wallets), wallets=wallets)
        return section
    except (OSError, KeyError, TypeError, ValueError, AttributeError, UnicodeError, OverflowError, RecursionError):
        return unavailable


def _status(context):
    enabled = [context[name] for name in ('personal', 'custody') if context[name]['status'] != 'disabled']
    usable = [section for section in enabled if section['status'] in ('ok', 'partial')]
    return ('ok' if enabled and len(usable) == len(enabled) and all(s['status'] == 'ok' for s in usable)
            else 'partial' if usable else 'unavailable')


def load_wallet_context(personal_path, custody_path, now):
    """Allowlisted balances only; each invalid source or row is excluded independently."""
    result = {'collection_mode': 'shadow', 'interpretation': INTERPRETATION,
              'personal': _section(personal_path, True, now),
              'custody': _section(custody_path, False, now)}
    result['status'] = _status(result)
    return result


def recheck_freshness(context, now):
    """Recheck at each model consumption, including retries, without modifying the builder's input."""
    result = copy.deepcopy(context)
    for name in ('personal', 'custody'):
        section = result.get(name, {})
        if section.get('status') not in ('ok', 'partial'):
            result[name] = {'status': 'disabled' if section.get('status') == 'disabled' else 'unavailable',
                            'available_wallets': 0}
            continue
        try:
            _fresh(section['generated_at'], now, MAX_AGE)
            age = _fresh(section['received_at'], now, MAX_AGE)
            if section['received_at'] > section['generated_at']:
                raise ValueError('inconsistent receipt')
            _anchor(section['anchor'], name == 'personal', section['received_at'], now)
            section['receipt_age_s'] = round(age, 1)
        except (KeyError, TypeError, ValueError, OverflowError):
            result[name] = {'status': 'unavailable', 'available_wallets': 0}
    result['status'] = _status(result)
    return result
