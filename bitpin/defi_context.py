"""Read-only daily macro context; no network, account, model or trading operations."""
import json
import math
import os
from pathlib import Path

from .flow_context import _age, _invalid_constant, _number, _read_regular, _unique

DAY = 86400
MAX_AGE = 9000
MAX_BYTES = 512 * 1024
CHAINS = ('Ethereum', 'Solana', 'BSC', 'Arbitrum', 'Sui', 'Tron')
METRICS = ('tvl', 'dex_volume', 'stablecoin_supply', 'fees', 'revenue', 'open_interest')
FLOWS = ('dex_volume', 'fees', 'revenue')
QUERY = '?excludeTotalDataChart=false&excludeTotalDataChartBreakdown=true'
INTERPRETATION = (
    'defi_daily contains supporting daily macro observations, not a validated trading edge. '
    'Use closed UTC dates only and compare 7/30-day trends with existing price, liquidity and risk evidence. '
    'change_pct is [1d,7d,30d]; coverage_days is [7d,30d]. Missing/stale values mean no evidence, never zero. '
    'TVL is affected by asset prices, protocol coverage and methodology; TVL change is not net capital inflow. '
    'USD-pegged token supply is a liquidity proxy, not proof of buying, and duplicates liquidity already '
    'shown in public_flow: do not count it as independent confirmation. DEX volume is gross turnover, not '
    'net buy/sell pressure. Fees/revenue describe covered protocol activity, not returns to every token holder. '
    'Open interest is a derivatives exposure stock: it has no long/short direction or leverage quality. '
    'No field identifies smart money or exchange deposits/withdrawals. Chain aggregates are not a token '
    'valuation or evidence for unrelated gold/stock tokens. Do not add a new setup/base-rate row, increase '
    'p beyond the existing P RULE allowance, bypass buy/confirmation/fee/stop gates or trigger extra '
    'decisions from these observations. Provider publication/finality is unknown; receipt time is not '
    'publication time and closed buckets can be revised. Never sum stock levels like daily flows, '
    'or add global and chain values together. Kimi remains the allocator under existing rules.'
)


def snapshot_path_valid(value):
    return (isinstance(value, str) and '\x00' not in value and os.path.isabs(value)
            and Path(value).name == 'defi-daily.json')


def _specs():
    # Fixed public collector provenance; no free-form snapshot URLs reach the model.
    for scope in ('global',) + CHAINS:
        for metric in METRICS if scope == 'global' else METRICS[:3]:
            suffix = '' if scope == 'global' else '/' + scope
            if metric == 'tvl':
                url = 'https://api.llama.fi/v2/historicalChainTvl' + suffix
            elif metric == 'stablecoin_supply':
                url = 'https://stablecoins.llama.fi/stablecoincharts/' + ('all' if scope == 'global' else scope)
            elif metric == 'dex_volume':
                url = 'https://api.llama.fi/overview/dexs' + suffix + QUERY + '&dataType=dailyVolume'
            elif metric == 'open_interest':
                url = 'https://api.llama.fi/overview/open-interest' + QUERY
            else:
                url = 'https://api.llama.fi/overview/fees' + QUERY + '&dataType=' + (
                    'dailyFees' if metric == 'fees' else 'dailyRevenue')
            yield metric + ':' + scope, metric, scope, url


def _pct(current, previous):
    if previous is None or previous <= 0:
        return None
    value = (current - previous) / previous * 100
    return round(value, 6) if math.isfinite(value) and abs(value) <= 1e20 else None


def _daily(record, metric, scope, url, now, generated):
    if (record['status'] != 'ok' or record['metric'] != metric or record['scope'] != scope
            or record['key'] != metric + ':' + scope or record['url'] != url):
        raise ValueError('source mismatch')
    received = _number(record['received_at'], True)
    age = _age(received, now, MAX_AGE)
    if received > generated:
        raise ValueError('future receipt')
    raw = record['features']
    unit = 'USD-pegged token units' if metric == 'stablecoin_supply' else 'USD'
    kind = 'daily_flow' if metric in FLOWS else 'stock_snapshot'
    if raw['quality'] != 'ok' or raw['unit'] != unit or raw['kind'] != kind or raw['is_net_inflow'] is not False:
        raise ValueError('unknown units or meaning')
    series = raw['series']
    if not isinstance(series, list) or not 1 <= len(series) <= 93:
        raise ValueError('unbounded history')
    collected_day = int(received // DAY) * DAY
    points = {}
    for row in series:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError('invalid row')
        stamp, value = _number(row[0], True), _number(row[1], metric not in ('fees', 'revenue'))
        if (stamp != int(stamp) or stamp % DAY
                or not collected_day - 92 * DAY <= stamp < collected_day or stamp in points):
            raise ValueError('open, duplicate or invalid UTC date')
        if abs(value) > 1e20:
            raise ValueError('invalid magnitude')
        points[int(stamp)] = value
    end = max(points)
    if (raw['as_of'] != end or isinstance(raw['as_of'], bool) or now - end > 3 * DAY
            or raw['value'] != points[end] or isinstance(raw['value'], bool)):
        raise ValueError('stale or inconsistent latest bucket')
    value = points[end]
    out = {'status': 'ok', 'as_of': end, 'received_at': received, 'receipt_age_s': round(age, 1), 'value': value,
           'change_pct': [_pct(value, points.get(end - days * DAY)) for days in (1, 7, 30)],
           'coverage_days': [sum(end - i * DAY in points for i in range(n)) for n in (7, 30)]}
    if metric in FLOWS:
        def mean(count, offset=0):
            values = [points.get(end - i * DAY) for i in range(offset, offset + count)]
            return sum(values) / count if all(v is not None for v in values) else None
        week, previous = mean(7), mean(7, 7)
        out.update(mean_7d=week, mean_30d=mean(30), week_over_week_pct=_pct(week, previous)
                   if week is not None else None)
    return out


def load_defi_daily(path, now):
    """Expose bounded validated facts; one bad source cannot contaminate the remaining sources."""
    unavailable = {'status': 'unavailable', 'interpretation': INTERPRETATION}
    try:
        if not snapshot_path_valid(path):
            return unavailable
        target = Path(path)
        if any(p.is_symlink() for p in (target.parent,) + tuple(target.parent.parents)):
            return unavailable
        if _read_regular(target.parent / '.defi-shadow-state', 64) not in (
                b'defillama-daily-shadow-v1\n', b'defillama-daily-shadow-v1\r\n'):
            return unavailable
        raw = json.loads(_read_regular(target, MAX_BYTES).decode('utf-8'),
                         parse_constant=_invalid_constant, object_pairs_hook=_unique)
        if (type(raw['schema_version']) is not int or raw['schema_version'] != 1 or raw['mode'] != 'shadow'
                or raw['live_enabled'] is not False or type(raw['model_calls']) is not int
                or raw['model_calls'] != 0 or type(raw['orders_created']) is not int or raw['orders_created'] != 0):
            return unavailable
        generated = _number(raw['generated_at'], True)
        age = _age(generated, now, MAX_AGE)
        if not isinstance(raw['sources'], dict):
            return unavailable
        sources = {}
        available = 0
        for key, metric, scope, url in _specs():
            try:
                sources[key] = _daily(raw['sources'][key], metric, scope, url, now, generated)
                available += 1
            except (ValueError, TypeError, KeyError, OverflowError):
                sources[key] = {'status': 'unavailable'}
        return {'status': 'ok' if available == len(sources) else 'partial' if available else 'unavailable',
                'provider': 'DefiLlama', 'collection_mode': 'shadow', 'generated_at': generated,
                'receipt_age_s': round(age, 1), 'provider_publication_time': None,
                'provider_finality': 'unknown', 'available_sources': available,
                'interpretation': INTERPRETATION, 'definitions': {
                    m: {'unit': 'USD-pegged token units' if m == 'stablecoin_supply' else 'USD',
                        'kind': 'daily_flow' if m in FLOWS else 'stock_snapshot'} for m in METRICS},
                'sources': sources}
    except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError, RecursionError):
        return unavailable


def recheck_freshness(context, now):
    """Recheck after context serialization/shrinking, without reading or fetching again."""
    try:
        if context.get('status') not in ('ok', 'partial'):
            return context
        age = _age(context['generated_at'], now, MAX_AGE)
        sources = context['sources']
        for key, row in list(sources.items()):
            if row.get('status') != 'ok':
                continue
            try:
                row['receipt_age_s'] = round(_age(row['received_at'], now, MAX_AGE), 1)
                if now - row['as_of'] > 3 * DAY:
                    raise ValueError('stale bucket')
            except (ValueError, TypeError, KeyError, OverflowError):
                sources[key] = {'status': 'unavailable'}
        count = sum(row.get('status') == 'ok' for row in sources.values())
        context.update(available_sources=count, receipt_age_s=round(age, 1),
                       status='ok' if count == len(sources) else 'partial' if count else 'unavailable')
        return context
    except (ValueError, TypeError, KeyError, OverflowError):
        return {'status': 'unavailable', 'interpretation': INTERPRETATION}
