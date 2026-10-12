"""Bounded citation contract for already sanitized optional public observations.

No file, network, account, model, or order operations occur here. The upstream
context loaders remain responsible for freshness and provenance. Every review
is tied to the facts actually supplied for this decision, including omissions.
"""
import copy
import hashlib
import json
import math
import re


SOURCE_NAMES = ('bitpin_flow', 'defi_daily', 'personal_wallets', 'custody_wallets')
OPTIONAL_GROUPS = ('public_flow', 'defi_daily', 'wallet_observations')
MAX_FACTS = 64
MAX_CONTRACT_CHARS = 12000
MAX_CITATIONS = 12
MAX_AFFECTED_SYMBOLS = 12
MAX_REFS = 12
MAX_WEI = 2 ** 256 - 1
MAX_SATS = 21000000 * 100000000
CHAIN_ASSETS = {'Ethereum': 'ETH', 'Solana': 'SOL', 'BSC': 'BNB',
                'Arbitrum': 'ETH', 'Sui': 'SUI', 'Tron': 'TRX'}
DEFI_METRICS = ('tvl', 'dex_volume', 'stablecoin_supply', 'fees', 'revenue', 'open_interest')
DEFI_FLOW_METRICS = ('dex_volume', 'fees', 'revenue')
ASSESSMENTS = ('supports', 'conflicts', 'mixed', 'neutral', 'unavailable')
EFFECTS = ('supports_existing', 'reduces_conviction', 'changes_selection', 'changes_size', 'none')
POLICY = {'supporting_only': True, 'independent_confirmations': False, 'probability_bonus': 0}
LIMITATIONS = {
    'bitpin_flow': [
        'Executed taker imbalance describes its exact market and quote unit; it is not deposits, withdrawals, or investor identity.',
        'Receipt time does not prove publication freshness or complete event coverage; missing or stale windows are no evidence.',
        'Stablecoin supply is a global liquidity proxy and duplicates DefiLlama supply; it is not independent buying confirmation.'],
    'defi_daily': [
        'TVL changes include price and coverage effects; DEX turnover is gross activity, not net buying or capital inflow.',
        'Supply is a liquidity proxy; fees and revenue do not imply holder returns; open interest has no long/short direction.',
        'Closed daily buckets can be revised; chain aggregates apply only to matching or explicitly related crypto assets.',
        'Global and chain aggregates overlap; stablecoin supply duplicates public-flow liquidity and is not independent confirmation.'],
    'personal_wallets': [
        'Public address association and current ownership or personal capital are unverified; native ETH excludes tokens, staking, and DeFi.',
        'Native balances and changes include transfers and fees, with no established buying direction; review must be neutral with no effect.',
        'Receipt-window deltas are incomplete transaction flows; wallets are not independent confirmations.'],
    'custody_wallets': [
        'Historical BTC custody address sample does not identify investors or complete exchange reserves; confirmation is not absolute finality.',
        'Native balances and changes include transfers and fees, with no established buying direction; review must be neutral with no effect.',
        'Receipt-window deltas are incomplete transaction flows; wallets are not independent confirmations.']}


class EvidenceContractError(ValueError):
    """Untrusted model evidence review does not match the submitted facts."""


def _obj(value):
    return value if type(value) is dict else {}


def _number(value):
    if type(value) is int:
        return abs(value) <= MAX_WEI
    return type(value) is float and math.isfinite(value) and abs(value) <= 1e30


def _scalar(value):
    return value is None or _number(value)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _symbols(context):
    return sorted(symbol for symbol in _obj(context.get('symbols'))
                  if type(symbol) is str and re.fullmatch(r'[A-Z0-9]{1,24}_IRT', symbol))


def _crypto_symbol(context, symbol):
    from .analysis import asset_class
    row = _obj(_obj(context.get('symbols')).get(symbol))
    declared = row.get('cls', row.get('asset_class'))
    return symbol == 'USDT_IRT' or (declared in (None, 'crypto') and asset_class(symbol) == 'crypto')


def _global_symbols(context):
    return [symbol for symbol in _symbols(context) if _crypto_symbol(context, symbol)]


def _asset_symbols(context, asset):
    return [symbol for symbol in _symbols(context) if symbol.split('_', 1)[0] == asset]


def _chain_symbols(context, network):
    asset = CHAIN_ASSETS[network]
    result = []
    for symbol in _symbols(context):
        row = _obj(_obj(context.get('symbols')).get(symbol))
        related = row.get('related_chains')
        if (_crypto_symbol(context, symbol) and
                (symbol.split('_', 1)[0] == asset or
                 (type(related) is list and network in related))):
            result.append(symbol)
    return result


def _fact(source, identity, fields, units, scope, symbols):
    return {'id': source + ':' + _digest(identity)[:24], 'fields': fields,
            'units': units, 'scope': scope, 'allowed_symbols': symbols}


def _flow_facts(context, packet):
    facts = []
    numeric = ('window_seconds', 'coverage_seconds', 'buy_quote', 'sell_quote', 'delta_quote',
               'buy_ratio', 'trade_count', 'pressure_ratio', 'imbalance_ratio',
               'imbalance_quote', 'mean_daily_delta_quote', 'as_of')
    for symbol, raw in sorted(_obj(packet.get('markets')).items(), key=lambda item: str(item[0])):
        row = _obj(raw)
        if (type(symbol) is not str or not re.fullmatch(r'[A-Z0-9]{1,24}_(IRT|USDT)', symbol)
                or row.get('status') != 'ready' or row.get('quote_unit') != symbol.rsplit('_', 1)[1]):
            continue
        for name in ('daily', 'multiday'):
            window = _obj(row.get(name))
            if not window or window.get('status', 'ready') != 'ready':
                continue
            if any(not _scalar(window[key]) for key in numeric if key in window):
                continue
            interval = window.get('window_seconds')
            if interval is not None and (not _number(interval) or interval < 86400):
                continue
            primary = ('buy_quote', 'sell_quote', 'delta_quote', 'buy_ratio', 'pressure_ratio',
                       'imbalance_ratio', 'imbalance_quote', 'mean_daily_delta_quote')
            if not any(window.get(key) is not None for key in primary):
                continue
            fields = {key: window[key] for key in numeric if key in window}
            if 'as_of' not in fields and _number(row.get('as_of')):
                fields['as_of'] = row['as_of']
            units = {key: ('seconds' if key.endswith('seconds') else 'unix_seconds' if key == 'as_of'
                           else 'count' if key == 'trade_count' else 'ratio' if key.endswith('ratio')
                           else row['quote_unit']) for key in fields}
            scope = {'kind': 'market_flow', 'market': symbol, 'window': name,
                     'quote_unit': row['quote_unit']}
            facts.append(_fact('bitpin_flow', [symbol, name], fields, units, scope,
                               _asset_symbols(context, symbol.split('_', 1)[0])))
    liquidity = _obj(packet.get('liquidity'))
    if liquidity.get('status') == 'ok' and liquidity.get('kind') == 'stablecoin_supply_proxy':
        for asset in ('USDT', 'USDC'):
            row = _obj(_obj(liquidity.get('assets')).get(asset))
            keys = ('circulating_units', 'change_1d_units', 'change_7d_units', 'change_30d_units')
            if (not _number(row.get('circulating_units')) or row['circulating_units'] <= 0
                    or any(not _scalar(row[key]) for key in keys if key in row)):
                continue
            fields = {key: row[key] for key in keys if key in row}
            units = {key: asset + ' token units' for key in fields}
            if _number(liquidity.get('received_at')):
                fields['received_at'] = liquidity['received_at']
                units['received_at'] = 'unix_seconds'
            facts.append(_fact('bitpin_flow', ['stablecoin_supply', asset], fields, units,
                {'kind': 'global_liquidity_proxy', 'asset': asset, 'network': 'global'}, _global_symbols(context)))
    return facts


def _defi_facts(context, packet):
    facts = []
    definitions = _obj(packet.get('definitions'))
    for key, raw in sorted(_obj(packet.get('sources')).items(), key=lambda item: str(item[0])):
        if type(key) is not str or key.count(':') != 1:
            continue
        metric, network = key.split(':')
        row = _obj(raw)
        if (metric not in DEFI_METRICS or network not in ('global',) + tuple(CHAIN_ASSETS)
                or (network != 'global' and metric not in DEFI_METRICS[:3]) or row.get('status') != 'ok'
                or not _number(row.get('value')) or not _number(row.get('as_of'))):
            continue
        unit = 'USD-pegged token units' if metric == 'stablecoin_supply' else 'USD'
        definition = _obj(definitions.get(metric))
        if definition and definition.get('unit') != unit:
            continue
        fields = {'value': row['value'], 'as_of': row['as_of']}
        units = {'value': unit, 'as_of': 'unix_seconds'}
        valid = True
        for field, expected_length in (('change_pct', 3), ('coverage_days', 2)):
            if field not in row:
                continue
            values = row[field]
            if type(values) is not list or len(values) != expected_length or any(not _scalar(v) for v in values):
                valid = False
                break
            for index, value in enumerate(values):
                path = field + '.' + str(index)
                fields[path] = value
                units[path] = 'percent' if field == 'change_pct' else 'days'
        for field in ('received_at', 'receipt_age_s', 'mean_7d', 'mean_30d', 'week_over_week_pct'):
            if field not in row:
                continue
            if not _scalar(row[field]):
                valid = False
                break
            if field in ('mean_7d', 'mean_30d', 'week_over_week_pct') and metric not in DEFI_FLOW_METRICS:
                continue
            fields[field] = row[field]
            units[field] = ('unix_seconds' if field == 'received_at' else 'seconds' if field == 'receipt_age_s'
                            else 'percent' if field == 'week_over_week_pct' else unit)
        if valid:
            scope = {'kind': 'daily_flow' if metric in DEFI_FLOW_METRICS else 'stock_snapshot',
                     'metric': metric, 'network': network}
            facts.append(_fact('defi_daily', [metric, network], fields, units, scope,
                               _global_symbols(context) if network == 'global' else _chain_symbols(context, network)))
    return facts


def _wallet_facts(context, section, personal):
    source, asset, unit, maximum = (('personal_wallets', 'ETH', 'wei', MAX_WEI) if personal else
                                    ('custody_wallets', 'BTC', 'sats', MAX_SATS))
    wallets = section.get('wallets')
    if type(wallets) is not list:
        return []
    facts = []
    used = set()
    for index, raw in enumerate(wallets):
        row = _obj(raw)
        balance_key = 'balance_' + unit
        balance = row.get(balance_key)
        if (row.get('status') != 'ok' or row.get('asset', asset) != asset
                or type(balance) is not int or not 0 <= balance <= maximum):
            continue
        address = row.get('address')
        if address is not None and (type(address) is not str or not 1 <= len(address) <= 128):
            continue
        identity = ['address', address] if address is not None else ['fixture_index', index]
        identifier = _digest(identity)
        if identifier in used:
            continue
        used.add(identifier)
        fields = {balance_key: balance}
        units = {balance_key: unit}
        for window, interval in (('24h', 86400), ('7d', 7 * 86400)):
            change_key, elapsed_key = 'change_' + window + '_' + unit, 'reference_' + window + '_elapsed_seconds'
            if change_key not in row:
                continue
            change, elapsed = row[change_key], row.get(elapsed_key)
            if change is None:
                fields[change_key] = None
                units[change_key] = unit
                continue
            if (type(change) is not int or abs(change) > maximum or not 0 <= balance - change <= maximum
                    or not _number(elapsed) or not interval - 3600 <= elapsed <= interval + 3600):
                continue
            fields[change_key] = change
            fields[elapsed_key] = elapsed
            units[change_key], units[elapsed_key] = unit, 'seconds'
        for key in ('received_at', 'generated_at'):
            if _number(section.get(key)):
                fields[key], units[key] = section[key], 'unix_seconds'
        anchor = _obj(section.get('anchor'))
        for key in ('number' if personal else 'height', 'timestamp'):
            if type(anchor.get(key)) is int and anchor[key] >= 0:
                fields['anchor.' + key] = anchor[key]
                units['anchor.' + key] = 'unix_seconds' if key == 'timestamp' else 'block_height'
        scope = {'kind': 'native_balance', 'asset': asset, 'network': 'Ethereum' if personal else 'Bitcoin',
                 'association': 'unverified'}
        facts.append(_fact(source, identity, fields, units, scope, _asset_symbols(context, asset)))
    return sorted(facts, key=lambda fact: fact['id'])


def _source(name, status, facts):
    return {'status': 'available' if facts else status, 'facts': facts,
            'limitations': list(LIMITATIONS[name]), 'available_fact_count': len(facts), 'omitted_fact_count': 0}


def _presence(context, group):
    if group in context:
        return 'unavailable'
    truncated = context.get('truncated')
    return 'omitted' if type(truncated) is list and group in truncated else 'absent'


def build_contract(context):
    """Generate from actual optional facts, ignoring any incoming cached catalog.

    The compact serialized catalog is capped at 12,000 characters and 64 facts;
    an exact integer evidence_catalog_max_chars in [4000, 12000] can reduce the
    character cap. The effective cap is included in the request identity.
    Counts describe eligible facts before catalog omission, not guessed upstream
    availability. Upstream context-budget omissions are separately labeled.
    """
    context = _obj(context)
    requested_cap = context.get('evidence_catalog_max_chars')
    catalog_cap = (requested_cap if type(requested_cap) is int and 4000 <= requested_cap <= MAX_CONTRACT_CHARS
                   else MAX_CONTRACT_CHARS)
    truncated = context.get('truncated')
    omitted = set(truncated) if type(truncated) is list and all(type(x) is str for x in truncated) else set()
    if 'decision_evidence' in omitted and not any(group in context for group in OPTIONAL_GROUPS):
        return None
    if not any(group in context or group in omitted for group in OPTIONAL_GROUPS):
        return None
    sources = {}
    flow = _obj(context.get('public_flow'))
    flow_facts = _flow_facts(context, flow) if flow.get('status') in ('ok', 'partial') else []
    sources['bitpin_flow'] = _source('bitpin_flow', _presence(context, 'public_flow'), flow_facts)
    defi = _obj(context.get('defi_daily'))
    defi_facts = _defi_facts(context, defi) if defi.get('status') in ('ok', 'partial') else []
    sources['defi_daily'] = _source('defi_daily', _presence(context, 'defi_daily'), defi_facts)
    if 'defi_daily_chains' in omitted:
        count = defi.get('omitted_chain_sources')
        sources['defi_daily']['upstream_omitted_source_count'] = count if type(count) is int and 0 <= count <= 18 else None
        sources['defi_daily']['limitations'].append('Chain sources were omitted by the upstream context budget.')
    wallets = _obj(context.get('wallet_observations'))
    for source, name, personal in (('personal_wallets', 'personal', True), ('custody_wallets', 'custody', False)):
        section = _obj(wallets.get(name))
        state = _presence(context, 'wallet_observations')
        if state == 'unavailable' and section.get('status') == 'disabled':
            state = 'absent'
        facts = (_wallet_facts(context, section, personal) if wallets.get('status') in ('ok', 'partial')
                 and section.get('status') in ('ok', 'partial') else [])
        sources[source] = _source(source, state, facts)
    # Hash all eligible numeric facts and source states, so omitted changed facts
    # cannot reuse a previous request. Portfolio/account numbers are excluded.
    contract = {'schema_version': 1,
                'request_id': _digest({'sources': sources, 'policy': POLICY, 'catalog_max_chars': catalog_cap}),
                'sources': sources, 'policy': dict(POLICY),
                'fact_count': sum(len(row['facts']) for row in sources.values()), 'omitted_fact_count': 0,
                'catalog_max_chars': catalog_cap}
    while contract['fact_count'] > MAX_FACTS or len(_canonical(contract)) > catalog_cap:
        # Preserve at least one citation target per available group where possible.
        active = [(name, row) for name, row in sources.items() if row['facts']]
        if not active:
            break
        name, row = max(active, key=lambda pair: (len(pair[1]['facts']), SOURCE_NAMES.index(pair[0])))
        row['facts'].pop()
        row['omitted_fact_count'] += 1
        contract['fact_count'] -= 1
        contract['omitted_fact_count'] += 1
        if not row['facts']:
            row['status'] = 'omitted'
    return contract


def attach_contract(context):
    """Return an independent copy with only a freshly generated code catalog."""
    result = copy.deepcopy(context)
    result.pop('decision_evidence', None)
    contract = build_contract(result)
    if contract is not None:
        result['decision_evidence'] = contract
    return result


def _error(message):
    raise EvidenceContractError('evidence_review: ' + message)


def _id_list(value, maximum, label):
    if (type(value) is not list or len(value) > maximum
            or any(type(item) is not str for item in value) or len(set(value)) != len(value)):
        _error(label + ' must be a bounded unique string list')
    return value


def validate_review(review, contract, allowed_symbols, candidates):
    """Validate exact citations, source states, asset scope, and candidate links.

    Returned state labels are authored here, never copied from model assertions.
    Citation values retain their exact JSON numeric type and native integers.
    Unknown top-level and source-row metadata is deliberately discarded; the
    returned object contains only the explicit fields built below. Citation
    objects reject unknown fields to prevent ambiguous field/unit claims.
    """
    if type(contract) is not dict or contract.get('schema_version') != 1 or type(contract.get('sources')) is not dict:
        _error('submitted contract is missing')
    if (type(review) is not dict or type(review.get('schema_version')) is not int or review['schema_version'] != 1
            or type(review.get('request_id')) is not str or review['request_id'] != contract.get('request_id')):
        _error('schema_version or request_id differs from the submitted contract')
    source_reviews = review.get('sources')
    if type(source_reviews) is not dict or set(source_reviews) != set(SOURCE_NAMES) or set(contract['sources']) != set(SOURCE_NAMES):
        _error('sources must contain exactly the four submitted source groups')
    if type(allowed_symbols) not in (list, tuple, set, frozenset) or any(type(s) is not str for s in allowed_symbols):
        _error('allowed_symbols is invalid')
    allowed = set(allowed_symbols)
    candidates = {} if candidates is None else candidates
    if type(candidates) is not dict:
        _error('candidates must be an object')
    catalog = {}
    for source in SOURCE_NAMES:
        packet = _obj(contract['sources'][source])
        facts = packet.get('facts')
        if packet.get('status') not in ('available', 'unavailable', 'absent', 'omitted') or type(facts) is not list:
            _error('invalid submitted source state')
        for fact in facts:
            if (type(fact) is not dict or type(fact.get('id')) is not str or fact['id'] in catalog
                    or type(fact.get('fields')) is not dict or type(fact.get('units')) is not dict
                    or set(fact['fields']) != set(fact['units'])
                    or any(type(unit) is not str for unit in fact['units'].values())
                    or any(not _scalar(value) for value in fact['fields'].values())
                    or type(fact.get('allowed_symbols')) is not list):
                _error('invalid submitted fact catalog')
            catalog[fact['id']] = (source, fact)
    sanitized = {'schema_version': 1, 'request_id': contract['request_id'], 'status': 'validated', 'sources': {}}
    cited = set()
    effects = []
    for source in SOURCE_NAMES:
        row = source_reviews[source]
        state = contract['sources'][source]['status']
        if type(row) is not dict:
            _error(source + ' must be an object')
        assessment, effect, reason = row.get('assessment'), row.get('effect'), row.get('reason')
        if type(assessment) is not str or assessment not in ASSESSMENTS or type(effect) is not str or effect not in EFFECTS:
            _error(source + ' assessment or effect is invalid')
        if type(reason) is not str or not 10 <= len(reason.strip()) <= 240:
            _error(source + ' reason must contain 10..240 characters')
        symbols = _id_list(row.get('affected_symbols'), MAX_AFFECTED_SYMBOLS, source + ' affected_symbols')
        if any(symbol not in allowed for symbol in symbols):
            _error(source + ' affected_symbols contains a disallowed asset')
        citations = row.get('citations')
        if type(citations) is not list or len(citations) > MAX_CITATIONS:
            _error(source + ' citations must be a bounded list')
        if state != 'available':
            if assessment != 'unavailable' or citations or symbols or effect != 'none':
                _error(source + ' unavailable or omitted evidence must have no citations or effect')
        elif not citations or assessment == 'unavailable':
            _error(source + ' available evidence requires an exact citation')
        if state == 'available' and source in ('personal_wallets', 'custody_wallets') and (assessment != 'neutral' or effect != 'none'):
            _error(source + ' native balances and changes require neutral assessment and no effect')
        if effect != 'none' and (not symbols or assessment == 'neutral'):
            _error(source + ' an effect requires affected assets and a non-neutral assessment')
        safe_citations, seen, source_ids = [], set(), set()
        for citation in citations:
            if type(citation) is not dict or not {'id', 'field', 'value'}.issubset(citation) or set(citation) - {'id', 'field', 'value', 'unit'}:
                _error(source + ' citation must contain id, field and value')
            identifier, field, value = citation['id'], citation['field'], citation['value']
            if type(identifier) is not str or type(field) is not str or identifier not in catalog:
                _error(source + ' citation id or field is unknown')
            owning_source, fact = catalog[identifier]
            if owning_source != source or field not in fact['fields']:
                _error(source + ' citation uses a different source or an unknown field path')
            expected = fact['fields'][field]
            if not _scalar(value) or type(value) is not type(expected) or value != expected:
                _error(source + ' citation value must exactly match the submitted numeric type and value')
            if 'unit' in citation and (type(citation['unit']) is not str or citation['unit'] != fact['units'][field]):
                _error(source + ' citation unit differs from the submitted unit')
            if (identifier, field) in seen:
                _error(source + ' citation field is duplicated')
            seen.add((identifier, field))
            source_ids.add(identifier)
            cited.add(identifier)
            safe_citations.append({'id': identifier, 'field': field, 'value': value})
        if state == 'available' and not any(_number(citation['value']) for citation in safe_citations):
            _error(source + ' requires at least one non-null numeric observation citation')
        for symbol in symbols:
            if not any(symbol in catalog[identifier][1]['allowed_symbols'] for identifier in source_ids):
                _error(source + ' affected asset is outside the cited evidence scope')
        if effect != 'none':
            effects.append((source, symbols, source_ids))
        sanitized['sources'][source] = {'status': state, 'assessment': assessment, 'citations': safe_citations,
            'affected_symbols': list(symbols), 'effect': effect, 'reason': reason.strip()}
    candidate_refs = {}
    for symbol, candidate in candidates.items():
        if type(candidate) is not dict or 'evidence_refs' not in candidate:
            continue
        refs = _id_list(candidate['evidence_refs'], MAX_REFS, str(symbol) + ' evidence_refs')
        if type(symbol) is not str or symbol not in allowed:
            _error('candidate evidence_refs uses a disallowed asset')
        for identifier in refs:
            if identifier not in catalog or identifier not in cited or symbol not in catalog[identifier][1]['allowed_symbols']:
                _error(symbol + ' evidence_refs contains unknown, uncited or unrelated evidence')
        candidate_refs[symbol] = set(refs)
    for source, symbols, source_ids in effects:
        for symbol in symbols:
            if symbol == 'USDT_IRT':
                continue
            linked = source_ids.intersection(candidate_refs.get(symbol, set()))
            if not linked or not any(symbol in catalog[identifier][1]['allowed_symbols'] for identifier in linked):
                _error(source + ' effect requires the affected candidate to reference its cited evidence')
    return sanitized
