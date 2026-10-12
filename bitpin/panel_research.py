# -*- coding: utf-8 -*-
"""Bounded public research reads and bilingual server-rendered presentation.

The helper calls collect with its installed app directory, never a request path.
No accounts, credentials, subprocesses, model calls or execution controls.
"""
import html
import json
import math
import os
import stat
from datetime import datetime, timezone

from .panel_i18n import Text, tr
from .personal_wallets import WALLETS

MAX_BYTES = 2 * 1024 * 1024
PUBLIC_PATHS = {
    'flow': '/var/lib/bitpin-flow-public/snapshot.json',
    'defi': '/var/lib/bitpin-defi-shadow/defi-daily.json',
    'whale': '/var/lib/bitpin-whale-shadow/whale-shadow.json',
    'personal_whale': '/var/lib/bitpin-personal-whale-shadow/personal-whale-shadow.json',
    'strategy': '/var/lib/bitpin-strategy-shadow/strategy-shadow.json',
}
LIVE_STATUS_PATH = '/var/lib/bitpin-bot/live-evidence-status.json'
CAPS = {'technical': .50, 'flow': .20, 'macro': .15, 'whale': .05, 'news': .10}
BTC_ADDRESSES = ('1Kr6QSydW9bFQG1mXiPNNu6WpJGmUa9i1g', '3JZq4atUahhuA9rLhXLMhhTo133J9rF97j',
                 'bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97')
NAMES = {
    'flow': Text('فشار خریدوفروش بیت‌پین', 'Bitpin buying and selling pressure'),
    'defi': Text('داده‌های روزانهٔ دیفای', 'Daily DeFi data'),
    'whale': Text('آدرس‌های امانی بیت‌کوین', 'BTC custody addresses'),
    'personal_whale': Text('آدرس‌های اتریوم منتسب به افراد', 'Publicly attributed ETH addresses'),
    'strategy': Text('آرشیو شواهد', 'Evidence archive'),
    'technical': Text('قیمت و روند', 'Price and trend'),
    'macro': Text('شرایط کلان و دیفای', 'Macro and DeFi conditions'),
    'news': Text('اخبار', 'News'),
}
METRICS = {
    'tvl': Text('ارزش قفل‌شده', 'Total value locked'),
    'dex_volume': Text('حجم معاملات غیرمتمرکز', 'DEX trading volume'),
    'stablecoin_supply': Text('عرضهٔ استیبل‌کوین', 'Stablecoin supply'),
    'fees': Text('کارمزدها', 'Fees'), 'revenue': Text('درآمد پروتکل‌ها', 'Protocol revenue'),
    'open_interest': Text('موقعیت‌های باز', 'Open interest'),
}
STATES = {
    'ok': Text('دادهٔ تازه', 'Fresh receipt'), 'missing': Text('داده موجود نیست', 'Missing data'),
    'invalid': Text('دادهٔ نامعتبر', 'Invalid data'), 'stale': Text('دادهٔ قدیمی یا زمان نامعتبر', 'Stale or invalid time'),
    'warming_up': Text('در حال جمع‌آوری سابقه', 'Warming up'), 'ready': Text('آماده', 'Ready'),
    'partial': Text('پوشش ناقص', 'Partial coverage'), 'error': Text('خطای منبع', 'Source error'),
    'disagreement': Text('عدم توافق منابع', 'Provider disagreement'),
    'collecting': Text('در حال دریافت', 'Collecting'), 'cooldown': Text('انتظار برای منبع', 'Provider cooldown'),
    'unknown': Text('نامشخص', 'Unknown'),
}


def _obj(value):
    return value if isinstance(value, dict) else {}


def _num(value):
    try:
        return value if type(value) in (float, int) and math.isfinite(value) else None
    except OverflowError:
        return None


def _int(value, signed=False):
    return value if type(value) is int and (signed or value >= 0) and abs(value) < 2 ** 256 else None


def _text(value, limit=100):
    return value[:limit] if isinstance(value, str) else ''


def _fresh(value, now, maximum):
    return _num(value) is not None and 0 <= now - value <= maximum


def _no_dup(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate field')
        value[key] = item
    return value


def _finite_float(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('nonfinite JSON')
    return value


def _read(path):
    """Linux walks directory descriptors with NOFOLLOW, including every ancestor.

    The Windows test fallback checks every path component and the opened file.
    Neither branch follows a final symlink, device, pipe, or oversized file.
    """
    path = os.path.abspath(path)
    fd = None
    parent = None
    try:
        if os.open in getattr(os, 'supports_dir_fd', set()) and hasattr(os, 'O_NOFOLLOW'):
            parent = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
            components = path.split(os.path.sep)
            for component in [p for p in components[:-1] if p]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        else:
            component = path
            while component:
                if stat.S_ISLNK(os.lstat(component).st_mode):
                    raise ValueError('symlink')
                ancestor = os.path.dirname(component)
                if ancestor == component:
                    break
                component = ancestor
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('not a bounded regular file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('oversized file')
        doc = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_dup,
                         parse_float=_finite_float, parse_constant=lambda s: _finite_float(s))
        if not isinstance(doc, dict):
            raise ValueError('object required')
        return doc, 'ok'
    except FileNotFoundError:
        return {}, 'missing'
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        return {}, 'invalid'
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


def _state(doc, read_status, now, maximum, receipt=False):
    if read_status != 'ok':
        return read_status
    if not _fresh(doc.get('generated_at'), now, maximum):
        return 'stale'
    if receipt and not _fresh(doc.get('received_at'), now, maximum):
        return 'stale'
    status = doc.get('status', 'ok')
    return status if isinstance(status, str) and status in STATES else 'invalid'


def _evaluation(doc):
    result = {'generated_at': _num(doc.get('generated_at')), 'fresh_kimi_status': _text(doc.get('fresh_kimi_status')),
              'quantitative': {}, 'recorded_kimi': {}, 'pilot': {}}
    for currency in ('IRT', 'USDT'):
        original = _obj(_obj(doc.get('quantitative')).get(currency))
        row = {'fold_count': len(original.get('folds')) if isinstance(original.get('folds'), list) else 0}
        for period in ('holdout', 'development'):
            part = _obj(original.get(period))
            selected = _text(part.get('selected_variant') or _obj(original.get('holdout')).get('selected_variant'))
            clean = {'selected_variant': selected, 'episode_count': _int(part.get('episode_count')),
                     'calendar_days': _int(part.get('calendar_days')), 'metrics': {}, 'comparators': {}}
            for variant in ('baseline', 'trend', 'mean_reversion', 'combined'):
                metrics = _obj(_obj(part.get('metrics')).get(variant))
                if metrics:
                    clean['metrics'][variant] = {k: _num(metrics.get(k)) for k in ('accuracy', 'brier', 'logloss')}
                comparator = _obj(_obj(part.get('comparators')).get(variant))
                if comparator:
                    clean['comparators'][variant] = {k: _num(comparator.get(k)) for k in
                        ('return', 'stress_return', 'gross_return', 'fee_per_side', 'slippage_per_side',
                         'stress_multiplier', 'cash_return', 'daily_equal_weight_long_return',
                         'daily_equal_weight_long_stress_return', 'daily_equal_weight_long_gross_return')}
                    clean['comparators'][variant]['currency'] = _text(comparator.get('currency'), 12)
                    clean['comparators'][variant]['method'] = _text(comparator.get('method'))
            benchmark = _obj(part.get('USDT_buy_hold_benchmark'))
            clean['USDT_buy_hold_benchmark'] = {
                'status': _text(benchmark.get('status')), 'currency': _text(benchmark.get('currency'), 12),
                **{k: _num(benchmark.get(k)) for k in ('return', 'gross_return', 'cost_per_side', 'calendar_days')}}
            row[period] = clean
        result['quantitative'][currency] = row
    recorded = _obj(doc.get('recorded_kimi'))
    result['recorded_kimi'] = {k: _int(recorded.get(k)) for k in
                             ('recorded_decisions', 'recorded_candidates', 'evaluated_candidates')}
    pilot = _obj(doc.get('pilot'))
    result['pilot'] = {k: _num(pilot.get(k)) for k in ('first_pair_worst_case_usd', 'worst_case_usd')}
    return result


def collect(app_dir, now):
    """Five public snapshots, sanitized delivery receipt, and installed results."""
    from .live_evidence import read_delivery_status
    result = {'generated_at': now, 'sources': {}, 'flow': [], 'defi': [], 'wallets': [], 'btc_wallets': [],
              'weights': {}, 'receipt_count': None, 'live_delivery': read_delivery_status(LIVE_STATUS_PATH, now)}
    docs = {}
    for key, path in PUBLIC_PATHS.items():
        doc, read_status = _read(path)
        status = _state(doc, read_status, now, 900 if key == 'flow' else 9000,
                        receipt=key in ('whale', 'personal_whale'))
        if status == 'ok' and ((key == 'flow' and not isinstance(doc.get('markets'), dict)) or
                             (key == 'defi' and not isinstance(doc.get('sources'), dict))):
            status = 'invalid'
        docs[key] = doc
        result['sources'][key] = {'status': status, 'generated_at': _num(doc.get('generated_at')),
                                 'received_at': _num(doc.get('received_at'))}
    for market, value in sorted(_obj(docs['flow'].get('markets')).items())[:24]:
        value = _obj(value)
        window = _obj(_obj(value.get('windows')).get('86400'))
        trades = _obj(value.get('trades'))
        status = result['sources']['flow']['status']
        if status == 'ok':
            status = _text(trades.get('status')) or 'missing'
            if status == 'ok' and not _fresh(trades.get('received_at'), now, 900):
                status = 'stale'
            if status == 'ok':
                status = _text(window.get('quality')) or 'missing'
                if status == 'ready' and (not _fresh(window.get('as_of'), now, 900) or
                                         _num(window.get('coverage_seconds')) != 86400):
                    status = 'partial'
                if status == 'ready':
                    buy, sell, pressure = (_num(window.get(k)) for k in ('buy_quote', 'sell_quote', 'pressure_ratio'))
                    count = _int(window.get('trade_count'))
                    if (value.get('quote_unit') != market.rsplit('_', 1)[-1] or
                            window.get('as_of') != trades.get('received_at') or
                            trades.get('received_at') > docs['flow'].get('generated_at') or
                            buy is None or sell is None or not 0 <= buy <= 1e30 or not 0 <= sell <= 1e30 or
                            count is None or count > 10**9 or (buy + sell == 0) != (count == 0) or
                            (buy + sell == 0 and pressure is not None) or
                            (buy + sell > 0 and (pressure is None or not -1 <= pressure <= 1 or
                             not math.isclose(pressure, (buy - sell) / (buy + sell), rel_tol=1e-9, abs_tol=1e-9)))):
                        status = 'invalid'
        result['flow'].append({'market': _text(market), 'unit': _text(value.get('quote_unit'), 12),
            'status': status, 'coverage_seconds': _num(window.get('coverage_seconds')),
            **{k: _num(window.get(k)) if status == 'ready' else None for k in
               ('buy_quote', 'sell_quote', 'pressure_ratio', 'trade_count')}})
    for key, value in sorted(_obj(docs['defi'].get('sources')).items())[:64]:
        value = _obj(value)
        feature = _obj(value.get('features'))
        status = result['sources']['defi']['status']
        if status == 'ok':
            status = _text(value.get('status')) or 'missing'
            if status == 'ok' and (not _fresh(value.get('received_at'), now, 9000) or
                                   not _fresh(feature.get('as_of'), now, 3 * 86400)):
                status = 'stale'
            if status == 'ok' and feature.get('quality') != 'ok':
                status = _text(feature.get('quality')) or 'invalid'
        result['defi'].append({'key': _text(key), 'metric': _text(value.get('metric')),
                              'scope': _text(value.get('scope')), 'unit': _text(feature.get('unit'), 64),
                              'status': status, 'as_of': _num(feature.get('as_of')),
                              **{k: _num(feature.get(k)) if status == 'ok' else None for k in
                                 ('value', 'change_1d_pct', 'change_7d_pct', 'change_30d_pct')}})
    for source, catalog, output, field in (('personal_whale', WALLETS, 'wallets', 'balance_wei'),
                                          ('whale', [{'address': a} for a in BTC_ADDRESSES], 'btc_wallets', 'balance_sats')):
        doc = docs[source]
        rows = doc.get('wallets')
        if not isinstance(rows, list):
            rows = [doc] if source == 'personal_whale' else []
        by_address = {r.get('address'): r for r in rows[:24] if isinstance(r, dict) and isinstance(r.get('address'), str)}
        provider_rows = _obj(doc.get('sources'))
        provider_keys = ('blockstream', 'mempool')
        if source == 'personal_whale':
            provider_keys = next((pair for pair in (('publicnode', 'drpc'), ('publicnode', 'flashbots'))
                                  if set(provider_rows) == set(pair)), ())
        for index, wallet in enumerate(catalog):
            original = _obj(by_address.get(wallet['address']))
            source_status = result['sources'][source]['status']
            if source == 'personal_whale' and provider_rows and not provider_keys:
                source_status = 'invalid'
            wallet_status = (_text(original.get('status')) or 'ok') if original else 'missing'
            usable = source_status == 'ok' and wallet_status == 'ok'
            row = {'address': wallet['address'], 'person': wallet.get('person', ''), 'index': index,
                   field: _int(original.get(field)) if usable else None,
                   'status': source_status if source_status != 'ok' else wallet_status if wallet_status != 'ok'
                             else 'ok' if _int(original.get(field)) is not None else 'missing'}
            for interval in ('24h', '7d'):
                key = 'change_' + interval + ('_wei' if source == 'personal_whale' else '_sats')
                row[key] = _int(original.get(key), True) if usable else None
            row['provider_statuses'] = {k: _text(_obj(provider_rows.get(k)).get('status')) or 'missing'
                                       for k in provider_keys}
            result[output].append(row)
    strategy = docs['strategy']
    packets = list(_obj(strategy.get('packets')).values())[:24]
    usable = result['sources']['strategy']['status'] == 'ok'
    for group, cap in CAPS.items():
        weights = [_num(_obj(_obj(p).get('weights')).get(group)) for p in packets] if usable else []
        valid = [w for w in weights if w is not None and 0 <= w <= cap]
        result['weights'][group] = {'cap': cap, 'maximum': max(valid) if valid and len(valid) == len(weights) else None,
            'eligible_packets': sum(_obj(_obj(p).get('coverage')).get(group) == 'eligible' for p in packets) if usable else 0,
            'packet_count': len(packets) if usable else 0}
    result['receipt_count'] = _int(strategy.get('receipt_count')) if usable else None
    evaluation, status = _read(os.path.join(app_dir, 'docs', 'evidence-strategy-results.json'))
    result['evaluation_status'] = status
    result['evaluation'] = _evaluation(evaluation) if status == 'ok' else {}
    return result


def _e(value):
    return html.escape(str(value), quote=True)


def _t(fa, en):
    return _e(tr(Text(fa, en)))


def _label(value, catalog):
    return _e(tr(catalog.get(value, Text(value or 'نامشخص', value or 'Unknown'))))


def _number(value, decimals=2, suffix=''):
    if _num(value) is None:
        return '<span class="research-unknown">%s</span>' % _t('نامشخص', 'Unknown')
    return '<bdi dir="ltr">%s%s</bdi>' % (format(value, ',.%df' % decimals), _e(suffix))


def _exact(value, decimals):
    if _int(value, True) is None:
        return _t('سابقه نامشخص', 'Unknown history')
    whole, fraction = divmod(abs(value), 10 ** decimals)
    return '<bdi dir="ltr">%s%s.%s</bdi>' % ('−' if value < 0 else '', format(whole, ','), str(fraction).zfill(decimals))


def _stamp(value):
    if _num(value) is None or not 0 <= value <= 253402214400:
        return _t('زمان نامشخص', 'Unknown time')
    return '<bdi dir="ltr">%s UTC</bdi>' % datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M')


def _badge(status):
    return '<span class="research-status %s">%s</span>' % ('good' if status in ('ok', 'ready') else 'limited',
                                                         _label(status, STATES))


def _section(title, inner, note=''):
    return '<section class="research-section"><div class="research-heading"><h2>%s</h2>%s</div>%s</section>' % (
        title, '<p>%s</p>' % note if note else '', inner)


def _table(head, rows):
    return '<div class="research-table"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
        ''.join('<th scope="col">%s</th>' % h for h in head),
        ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % c for c in row) for row in rows))


def _delivery(value):
    """Describe transport of supporting facts, without inferring model influence."""
    value = _obj(value)
    status, stage = value.get('status', 'missing'), value.get('pipeline_stage')
    submitted = status == 'ok' and stage in ('attempted', 'response_received')
    stages = {
        'attempted': Text('تلاش برای ارسال درخواست', 'Request attempted'),
        'response_received': Text('پاسخ دریافت شد', 'Response received'),
        'not_called': Text('ارسال نشده', 'Not submitted'),
    }
    stage_text = (_label(stage, stages) if status == 'ok' and stage in stages else
                  _t('ارسال فعلی نامشخص', 'Current delivery unknown'))
    sources = _obj(value.get('sources'))
    cards = []
    for key, label in (
        ('public_flow', NAMES['flow']), ('defi_daily', NAMES['defi']),
        ('wallet_observations', Text('مشاهدات کیف‌پول‌ها', 'Wallet observations'))):
        row = _obj(sources.get(key))
        presence = row.get('presence')
        usable = _int(row.get('usable_count')) if submitted else None
        presence_labels = {
            'included': Text('در متن تحلیل موجود', 'Included in context'),
            'omitted': Text('از متن تحلیل حذف شده', 'Omitted from context'),
            'absent': Text('در متن تحلیل وجود ندارد', 'Absent from context'),
        }
        presence_text = (_label(presence, presence_labels) if submitted and presence in presence_labels else
                         _t('ارسال نشده', 'Not submitted') if status == 'ok' and stage == 'not_called' else
                         _t('ارسال فعلی نامشخص', 'Current delivery unknown'))
        good = (submitted and presence == 'included' and row.get('status') in ('ok', 'partial') and
                usable is not None and usable > 0)
        quality = _t('مشاهدهٔ قابل استفاده ندارد', 'No usable observations') if submitted and usable == 0 else (
            _t('پوشش ناقص', 'Partial coverage') if submitted and row.get('status') == 'partial' else '')
        cards.append('<article class="research-delivery-source"><h3>%s</h3>'
                     '<span class="research-status %s">%s</span><dl><div><dt>%s</dt><dd>%s</dd></div></dl>%s</article>' % (
            _e(tr(label)), 'good' if good else 'limited', presence_text,
            _t('مشاهدات قابل استفاده', 'Usable observations'), _number(usable, 0),
            '<p>%s</p>' % quality if quality else ''))
    return ('<section class="research-section research-delivery" id="research-live-delivery">'
            '<div class="research-heading"><h2>%s</h2><p>%s</p></div>'
            '<div class="research-delivery-meta"><span class="research-status %s">%s</span>'
            '%s<span>%s: %s</span></div><div class="research-delivery-sources">%s</div>'
            '<p class="research-delivery-note">%s</p></section>') % (
        _t('آخرین ارسال شواهد به کیمی', 'Last Kimi context delivery'),
        _t('وضعیت متنِ آخرین درخواست، پس از کنترل کیفیت و محدودیت حجم؛ مقدار حذف‌شده به کیمی نرسیده است.',
           'Last request context, after quality checks and prompt limits. Omitted observations did not reach Kimi.'),
        'good' if status == 'ok' and stage == 'response_received' else 'limited', stage_text,
        '' if status == 'ok' else _badge(status),
        _t('زمان ثبت', 'Recorded at'), _stamp(value.get('generated_at')),
        ''.join(cards),
        _t('دریافت پاسخ فقط بازگشت مدل را ثبت می‌کند؛ ثابت نمی‌کند که مدل از این داده‌ها استفاده کرده یا سود ساخته است. نبود گزارش، نشانهٔ خاموش‌بودن تنظیمات نیست.',
           'A returned response does not prove the model acted on the facts or earned a profit. Missing delivery records do not establish that settings are off.'))


def render(data, currency='IRT', period='holdout'):
    """Authenticated panel content; all remote fields are escaped, no inline CSS/JS."""
    data = _obj(data)
    evaluation = _obj(data.get('evaluation'))
    out = ['<div class="research-page"><section class="research-hero"><div><p class="research-eyebrow">%s</p>'
           '<h2>%s</h2><p>%s</p></div><span class="research-status limited">%s</span></section>' % (
        _t('بیت‌پین · آزمایش شواهد', 'BITPIN · EVIDENCE RESEARCH'),
        _t('داده‌ها را ببین، اثرگذاری را بسنج', 'Observe data. Measure influence.'),
        _t('این صفحه داده‌های عمومی و نتیجهٔ آزمایش‌ها را نشان می‌دهد. تصمیم‌گیر واقعی همچنان کیمی است.',
           'Public observations and measured experiments. Kimi remains the live decision maker.'),
        _t('تحلیل واقعی و پژوهش آزمایشی', 'Live context and experimental research'))]
    source_cards = []
    for key in PUBLIC_PATHS:
        row = _obj(_obj(data.get('sources')).get(key))
        source_cards.append('<article class="research-source"><h3>%s</h3>%s<p>%s</p></article>' % (
            _label(key, NAMES), _badge(row.get('status', 'missing')), _stamp(row.get('generated_at'))))
    out.append('<div class="research-sources">%s</div>' % ''.join(source_cards))
    out.append(_delivery(data.get('live_delivery')))
    weight_cards = []
    for key, cap in CAPS.items():
        row = _obj(_obj(data.get('weights')).get(key))
        weight = row.get('maximum')
        group_name = _t('کیف‌پول‌های بزرگ', 'Large-wallet observations') if key == 'whale' else _label(key, NAMES)
        weight_cards.append('<article class="research-weight"><h3>%s</h3><div class="research-large">%s</div>'
                            '<p>%s</p><div class="research-meter"><span class="cap-%s"></span></div>'
                            '<p>%s <strong>%s</strong></p><small><bdi dir="ltr">%s / %s</bdi> %s</small></article>' % (
            group_name, _number(weight * 100 if _num(weight) is not None else None, 1, '%'),
            _t('بیشترین وزن آزمایشی در بسته‌های دارایی', 'Maximum experimental weight across asset packets'), key,
            _t('سقف پژوهشی:', 'Research ceiling:'), _number(cap * 100, 0, '%'),
            _number(row.get('eligible_packets'), 0), _number(row.get('packet_count'), 0),
            _t('بسته با دادهٔ واجد شرایط', 'packets with eligible evidence')))
    out.append(_section(_t('وزن‌های آزمایشی پیش‌بینی', 'Experimental forecast weights'),
        '<div class="research-weights">%s</div>' % ''.join(weight_cards),
        _t('این وزن‌ها فقط برای ترکیب آزمایشی پیش‌بینی‌ها هستند و میزان توجه کیمی یا تخصیص واقعی را تعیین نمی‌کنند. سقف‌ها فرض پژوهشی‌اند؛ وزن آموزش‌ندیده به مدل پایه برمی‌گردد.',
           'These weights do not set Kimi\'s attention or live allocation. They belong to a research-only forecast mixture. Ceilings are hypotheses; untrained weights return to baseline.')))
    flow_rows = []
    for row in data.get('flow', []):
        flow_rows.append(['<bdi dir="ltr">%s</bdi>' % _e(row.get('market', '')), _badge(row.get('status')),
            _number(row.get('coverage_seconds', 0) / 3600 if _num(row.get('coverage_seconds')) is not None else None, 1, ' / 24h'),
            _number(row.get('buy_quote')), _number(row.get('sell_quote')),
            _number(row.get('pressure_ratio') * 100 if _num(row.get('pressure_ratio')) is not None else None, 1, '%'),
            '<bdi dir="ltr">%s</bdi>' % _e(row.get('unit', ''))])
    out.append(_section(_label('flow', NAMES), _table([
        _t('بازار', 'Market'), _t('کیفیت', 'Quality'), _t('پوشش روزانه', 'Daily coverage'),
        _t('خرید تهاجمی', 'Aggressive buys'), _t('فروش تهاجمی', 'Aggressive sells'),
        _t('فشار خالص', 'Net pressure'), _t('واحد', 'Unit')], flow_rows) if flow_rows else
        '<p class="research-empty">%s</p>' % _t('هنوز دادهٔ روزانهٔ قابل نمایش نداریم.', 'No daily observations available.'),
        _t('فشار خریدوفروش از معاملات مشاهده‌شده است، نه ورود و خروج پول به صرافی. پوشش ناقص یا قدیمی به‌صورت نامشخص نمایش داده می‌شود.',
           'Pressure describes observed trades, not exchange cash inflows. Incomplete or stale windows remain unknown.')))
    defi_rows = []
    for row in data.get('defi', []):
        defi_rows.append([_label(row.get('metric'), METRICS), _t('کل بازار', 'Global') if row.get('scope') == 'global' else
            '<bdi dir="ltr">%s</bdi>' % _e(row.get('scope', '')), _badge(row.get('status')),
            _number(row.get('value')), '<bdi dir="ltr">%s</bdi>' % _e(row.get('unit', '')),
            _number(row.get('change_7d_pct'), 2, '%'), _number(row.get('change_30d_pct'), 2, '%'), _stamp(row.get('as_of'))])
    out.append(_section(_t('شاخص‌های رایگان DefiLlama', 'Free DefiLlama indicators'), _table([
        _t('شاخص', 'Indicator'), _t('محدوده', 'Scope'), _t('کیفیت', 'Quality'), _t('مقدار', 'Value'),
        _t('واحد', 'Unit'), _t('تغییر ۷ روزه', '7-day change'), _t('تغییر ۳۰ روزه', '30-day change'),
        _t('روز داده', 'Data date')], defi_rows) if defi_rows else
        '<p class="research-empty">%s</p>' % _t('هنوز شاخصی دریافت نشده است.', 'No indicators received.'),
        _t('TVL اثر تغییر قیمت را هم دارد؛ حجم معاملات جهت خرید را نشان نمی‌دهد؛ عرضهٔ استیبل‌کوین معادل ورود خالص پول نیست.',
           'TVL includes price effects; volume does not establish buying direction; stablecoin supply is not net cash inflow.')))
    for key, output, balance_key, decimals in [('personal_whale', 'wallets', 'balance_wei', 18),
                                               ('whale', 'btc_wallets', 'balance_sats', 8)]:
        cards = []
        for row in data.get(output, []):
            index = row.get('index', 0)
            title = _e(row.get('person')) if key == 'personal_whale' else (
                _t('آدرس امانی گرم', 'Published hot custody address') if index == 0 else
                _t('آدرس امانی سرد', 'Published cold custody address') + ' ' + str(index))
            suffix = '_wei' if key == 'personal_whale' else '_sats'
            caveat = _t('انتساب عمومی؛ مالکیت فعلی و شخصی‌بودن سرمایه تأیید نشده است.',
                        'Public association; current ownership and personal capital are unverified.')
            if key == 'personal_whale' and index == 2:
                caveat = _t('انتساب عمومی با اعتبار متوسط؛ سرمایهٔ شخصی یا صندوق مشخص نیست.',
                            'Medium public attribution; personal versus fund capital is unclear.')
            if key == 'whale':
                caveat = _t('نمونهٔ تاریخی منتشرشده توسط Bitfinex؛ نهنگ مستقل یا کل ذخایر صرافی نیست.',
                            'Historical Bitfinex custody sample; not independent investors or complete reserves.')
            cards.append('<article class="research-wallet"><div class="research-wallet-head"><h3>%s</h3>%s</div>'
                '<p class="research-address"><bdi dir="ltr">%s</bdi></p><div class="research-balance">%s <small>%s</small></div>'
                '<dl><div><dt>%s</dt><dd>%s</dd></div><div><dt>%s</dt><dd>%s</dd></div></dl><p class="research-caveat">%s</p>'
                '<p class="research-providers">%s</p></article>' % (title, _badge(row.get('status')),
                    _e(row.get('address', '')), _exact(row.get(balance_key), decimals), 'ETH' if decimals == 18 else 'BTC',
                    _t('تغییر ۲۴ ساعته', '24-hour change'), _exact(row.get('change_24h' + suffix), decimals),
                    _t('تغییر ۷ روزه', '7-day change'), _exact(row.get('change_7d' + suffix), decimals), caveat,
                    ' · '.join('<bdi dir="ltr">%s</bdi>: %s' % (_e(provider), _label(status, STATES))
                               for provider, status in _obj(row.get('provider_statuses')).items())))
        out.append(_section(_label(key, NAMES), '<div class="research-wallets">%s</div>' % ''.join(cards),
            _t('فقط موجودی بومی زنجیره؛ انتقال، کارمزد و تأمین سرمایه با خریدوفروش فرق دارند. مزیت پیش‌بینی اثبات نشده است.',
               'Native balances only. Transfers, fees and funding are distinct from trades. Predictive edge is unproven.')))
    selectors = []
    for name, label, choices, chosen in (
        ('currency', Text('مبنای بازده', 'Return currency'), [('IRT', Text('تومان', 'Toman')), ('USDT', Text('تتر', 'USDT'))], currency),
        ('period', Text('بازهٔ آزمایش', 'Evaluation period'), [('holdout', Text('آزمون نهایی', 'Final holdout')),
                                                             ('development', Text('مراحل توسعه', 'Development folds'))], period)):
        selectors.append('<label for="research-%s">%s<select id="research-%s" name="%s">%s</select></label>' % (
            name, _e(tr(label)), name, name, ''.join('<option value="%s"%s>%s</option>' % (
                value, ' selected' if chosen == value else '', _e(tr(text))) for value, text in choices)))
    controls = '<form method="get" action="/research" class="research-controls">%s<button class="btn primary" type="submit">%s</button></form>' % (
        ''.join(selectors), _t('نمایش نتیجه', 'Show results'))
    quant = _obj(_obj(evaluation.get('quantitative')).get(currency))
    selected_period = _obj(quant.get(period))
    selected = selected_period.get('selected_variant')
    metrics = _obj(selected_period.get('metrics'))
    rows = []
    variants = {'baseline': Text('مدل پایه', 'Baseline'), 'trend': Text('روند', 'Trend'),
                'mean_reversion': Text('بازگشت به میانگین', 'Mean reversion'), 'combined': Text('ترکیبی', 'Combined')}
    for variant, metric in metrics.items():
        rows.append([_label(variant, variants) + (' <span class="research-status good">%s</span>' %
                     _t('منتخب', 'Selected') if variant == selected else ''),
            _number(metric.get('accuracy') * 100 if _num(metric.get('accuracy')) is not None else None, 2, '%'),
            _number(metric.get('brier'), 6), _number(metric.get('logloss'), 6)])
    result_html = controls + '<div class="research-result-meta"><span>%s: %s</span><span>%s: %s</span><span>%s: %s</span></div>' % (
        _t('پیش‌بینی', 'Forecasts'), _number(selected_period.get('episode_count'), 0),
        _t('روز تقویمی', 'Calendar days'), _number(selected_period.get('calendar_days'), 0),
        _t('مرحلهٔ توسعه', 'Development folds'), _number(quant.get('fold_count'), 0))
    result_html += _table([_t('مدل', 'Model'), _t('دقت جهت', 'Directional accuracy'), 'Brier ↓', 'Log loss ↓'], rows) if rows else (
        '<p class="research-empty">%s</p>' % _t('نتیجهٔ معتبر موجود نیست.', 'No valid results available.'))
    comparator = _obj(_obj(selected_period.get('comparators')).get(selected))
    result_html += '<div class="research-simulation"><h3>%s</h3><div class="research-large">%s</div><p>%s</p></div>' % (
        _t('شبیه‌سازی، نه زیان حساب واقعی', 'Simulation, not live account loss'),
        _number(comparator.get('return') * 100 if _num(comparator.get('return')) is not None else None, 2, '%'),
        _t('مقایسهٔ خرید یا نقد با بستن و بازکردن اجباری روزانه، پس از کارمزد و لغزش قیمت؛ بازپخش کامل ربات نیست.',
           'Daily long-or-cash comparator with mandatory daily closing and reopening, after fees and slippage; not a full bot replay.'))
    benchmark = _obj(selected_period.get('USDT_buy_hold_benchmark'))
    finance_rows = []
    def pct(value):
        return _number(value * 100 if _num(value) is not None else None, 2, '%')
    for label, gross, net, stress in (
        (Text('مدل منتخب با معاملهٔ روزانه', 'Selected daily comparator'), comparator.get('gross_return'),
         comparator.get('return'), comparator.get('stress_return')),
        (Text('مرجع روزانهٔ وزن برابر', 'Daily equal-weight reference'), comparator.get('daily_equal_weight_long_gross_return'),
         comparator.get('daily_equal_weight_long_return'), comparator.get('daily_equal_weight_long_stress_return')),
        (Text('مرجع وجه نقد', 'Cash reference'), comparator.get('cash_return'), comparator.get('cash_return'), comparator.get('cash_return'))):
        finance_rows.append([_e(tr(label)), pct(gross), pct(net), pct(stress)])
    result_html += '<div class="research-costs"><h3>%s</h3><p>%s %s · %s %s · %s %s</p>%s' % (
        _t('مقایسه‌های مالی با هزینه‌های اعلام‌شده', 'Financial references with declared costs'),
        _t('کارمزد هر سمت:', 'Fee per side:'),
        _number(comparator.get('fee_per_side') * 100 if _num(comparator.get('fee_per_side')) is not None else None, 3, '%'),
        _t('لغزش هر سمت:', 'Slippage per side:'),
        _number(comparator.get('slippage_per_side') * 100 if _num(comparator.get('slippage_per_side')) is not None else None, 3, '%'),
        _t('ضریب هزینه در حالت سخت:', 'Stress cost multiplier:'), _number(comparator.get('stress_multiplier'), 0, '×'),
        _table([_t('مرجع', 'Reference'), _t('بازده ناخالص', 'Gross return'),
                _t('پس از هزینه‌ها', 'After costs'), _t('هزینهٔ دوبرابر', 'Double-cost stress')], finance_rows))
    if benchmark.get('status') == 'evaluated' and benchmark.get('currency') == currency:
        # Format the endpoint benchmark separately from mandatory daily reopening.
        result_html += ('<div class="research-endpoint"><h3>%s</h3><p>%s: %s · %s: <bdi dir="ltr">%s</bdi>'
                        ' · %s: %s</p><p>%s</p></div>') % (
            _t('خرید و نگهداری تتر', 'USDT buy and hold'), _t('بازده پس از هزینه', 'Return after costs'),
            pct(benchmark.get('return')), _t('مبنای بازده', 'Return currency'), _e(currency),
            _t('هزینهٔ هر سمت', 'Cost per side'),
            _number(benchmark.get('cost_per_side') * 100 if _num(benchmark.get('cost_per_side')) is not None else None, 3, '%'),
            _t('فقط هزینهٔ ورود و خروج؛ نگهداری در کل بازه، بدون بستن و بازکردن روزانه. این مرجع با شبیه‌سازی بالا روش متفاوتی دارد.',
               'Endpoint costs only; held for the whole period without daily reopening. This benchmark uses a different method from the daily simulation.'))
    result_html += '</div>'
    out.append(_section(_t('نتایج آزمایش تاریخی', 'Historical evaluation'), result_html,
        _t('این آزمایش فقط قیمت را بررسی می‌کند. پوشش تاریخی هم‌زمان برای جریان سفارش، دیفای و نهنگ‌ها نداریم؛ بهبود اندک احتمال‌ها سودآوری را ثابت نمی‌کند.',
           'This is price-only research. Historical receipt coverage for flow, DeFi and wallets is unavailable; small probability improvements do not prove profitable trades.')))
    recorded = _obj(evaluation.get('recorded_kimi'))
    pilot = _obj(evaluation.get('pilot'))
    kimi = '<div class="research-kimi"><article><h3>%s</h3><p>%s: %s · %s: %s</p><strong>%s</strong><p>%s</p></article>' % (
        _t('پاسخ‌های قبلی کیمی', 'Recorded Kimi responses'), _t('تصمیم ثبت‌شده', 'Recorded decisions'),
        _number(recorded.get('recorded_decisions'), 0), _t('نتیجهٔ قابل ارزیابی', 'Evaluable outcomes'),
        _number(recorded.get('evaluated_candidates'), 0), _t('دقت قابل محاسبه نیست', 'Accuracy cannot be calculated'),
        _t('صفر نتیجهٔ قابل ارزیابی به معنی دقت صفر درصد نیست؛ برنامه‌ها و دادهٔ همان زمان کامل نیستند.',
           'Zero evaluable outcomes is not zero-percent accuracy; contemporaneous plans and data are incomplete.'))
    kimi += '<article><h3>%s</h3><strong>%s</strong><p>%s</p><p>%s: %s · %s: %s</p></article></div>' % (
        _t('آزمایش جدید کیمی', 'Fresh Kimi experiment'), _t('اجرا نشده', 'Not run') if evaluation.get('fresh_kimi_status') == 'not_run'
        else _t('وضعیت تأیید نشده', 'Status unverified'),
        _t('سقف کلی هزینه ۱۰ دلار است؛ بودجه رزرو سقف است، نه هزینهٔ پرداخت‌شده. پاسخ مفید تضمین نمی‌شود. اجرای این صفحه فقط خواندنی است.',
           '$10 total spending ceiling; reservation estimates are ceilings, not incurred costs. Useful responses are not guaranteed. This page is read-only.'),
        _t('حداکثر دو درخواست اول', 'First pair ceiling'), _number(pilot.get('first_pair_worst_case_usd'), 3, ' USD'),
        _t('حداکثر کل آزمایش ثبت‌شده', 'Registered experiment ceiling'), _number(pilot.get('worst_case_usd'), 3, ' USD'))
    out.append(_section(_t('کیفیت ارزیابی کیمی', 'Kimi evaluation quality'), kimi))
    out.append('<p class="research-footer">%s · %s: %s</p></div>' % (
        _t('وضعیت ارسال واقعی در بالا ثبت می‌شود؛ مزیت پیش‌بینی هنوز اثبات نشده است.',
           'Live context delivery is recorded above; predictive benefit remains unproven.'),
        _t('زمان گزارش تاریخی', 'Historical report generated'), _stamp(evaluation.get('generated_at'))))
    return ''.join(out)
