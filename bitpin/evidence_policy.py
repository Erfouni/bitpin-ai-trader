"""Typed, time-gated experimental evidence packets. No trading dependencies."""
import hashlib
import json
import math
from .evidence_statistics import normalize_probs

CAPS = {'technical': .50, 'flow': .20, 'macro': .15, 'whale': .05, 'news': .10}
TARGET = 'next_24h_down_flat_up'
COST_BAND = .008


def mixture(base, forecasts, weights, caps=CAPS):
    base = normalize_probs(base)
    effective = {g: 0. for g in caps}
    for group, weight in weights.items():
        if group not in caps or isinstance(weight,bool) or not isinstance(weight,(int,float)) or not math.isfinite(weight) or not 0 <= weight <= caps[group]:
            raise ValueError('invalid group influence')
        if group in forecasts:
            normalize_probs(forecasts[group])
            effective[group] = float(weight)
    total = sum(effective.values())
    if total > 1+1e-12:
        raise ValueError('influence exceeds one')
    remainder = max(0.,1-total)
    probs = [remainder*p for p in base]
    for group,weight in effective.items():
        if weight:
            for i,p in enumerate(normalize_probs(forecasts[group])):
                probs[i] += weight*p
    return {'probabilities': normalize_probs(probs), 'weights': effective, 'baseline_weight': remainder}


def _finite(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)


def _quality(row, cutoff):
    dates = [row.get(k) for k in ('event_time','received_at','first_observed_at')]
    if not all(_finite(v) and v >= 0 for v in dates):
        return 'invalid_time'
    if any(v > cutoff for v in dates):
        return 'not_available_as_of'
    if row.get('status') != 'ok':
        return str(row.get('status','missing'))[:40]
    age = row.get('max_age')
    if not _finite(age) or age <= 0 or cutoff-dates[0] > age or cutoff-dates[1] > age:
        return 'stale'
    receipt_age=row.get('receipt_max_age',age)
    if not _finite(receipt_age) or receipt_age <= 0 or cutoff-dates[1] > receipt_age:
        return 'stale'
    digest = row.get('sha256')
    if not isinstance(digest,str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        return 'invalid_provenance'
    if not row.get('unit') or not row.get('source') or not row.get('fact_id'):
        return 'invalid_identity'
    metrics = row.get('metrics')
    if not isinstance(metrics,dict) or not metrics or any(not _finite(v) for v in metrics.values()):
        return 'invalid_metrics'
    return 'eligible'


def assemble_packet(cutoff, symbol, observations, baseline, forecasts=None, influence=None, return_currency='IRT'):
    if not _finite(cutoff) or cutoff < 0 or not isinstance(symbol,str) or not symbol:
        raise ValueError('valid cutoff and symbol required')
    forecasts, influence = forecasts or {}, influence or {}
    if return_currency not in ('IRT','USDT'):
        raise ValueError('explicit supported return currency required')
    baseline = normalize_probs(baseline)
    selected, future = {}, []
    for raw in observations:
        row = dict(raw)
        group = row.get('group')
        if group not in CAPS or row.get('symbol') not in (symbol,'GLOBAL'):
            continue
        key = (group,row.get('source'),row.get('symbol'),row.get('fact_id'))
        dates = (row.get('received_at'),row.get('first_observed_at'))
        if not all(_finite(v) and v <= cutoff for v in dates):
            future.append(row)
            continue
        old = selected.get(key)
        if old is None or row['received_at'] >= old['received_at']:
            selected[key] = row
    evidence = []
    coverage = {g:'not_available_as_of' for g in CAPS}
    for key,row in sorted(selected.items(),key=lambda item: repr(item[0])):
        status = _quality(row,cutoff)
        safe = {k:row.get(k) for k in ('group','source','symbol','fact_id','unit','event_time','received_at','first_observed_at','max_age','sha256')}
        for k in ('event_time','received_at','first_observed_at','max_age'):
            if not _finite(safe[k]): safe[k]=None
        safe['status'] = status
        safe['metrics'] = row.get('metrics') if status == 'eligible' else {}
        evidence.append(safe)
        group = row['group']
        if status == 'eligible' or coverage[group] != 'eligible':
            coverage[group] = status
    available, weights, reasons = {}, {}, {}
    mature = (_finite(influence.get('max_label_end')) and influence['max_label_end'] <= cutoff
              and _finite(influence.get('sample_count')) and influence['sample_count'] >= 30
              and _finite(influence.get('calibration_calendar_days')) and influence['calibration_calendar_days'] >= 30
              and influence.get('target') == TARGET and influence.get('cost_band') == COST_BAND
              and influence.get('return_currency') == return_currency)
    for group in CAPS:
        forecast = forecasts.get(group) or {}
        reason = coverage[group]
        if reason == 'eligible':
            reason = 'untrained' if not mature else 'missing_forecast'
            if mature and forecast:
                if forecast.get('target') != TARGET or forecast.get('cost_band') != COST_BAND:
                    reason = 'target_mismatch'
                elif (forecast.get('symbol') != symbol or forecast.get('return_currency') != return_currency
                      or not _finite(forecast.get('decision_time')) or forecast['decision_time'] != cutoff
                      or not _finite(forecast.get('label_end')) or forecast['label_end'] != cutoff+86400
                      or not _finite(forecast.get('received_at')) or forecast['received_at'] > cutoff):
                    reason = 'identity_mismatch'
                else:
                    try:
                        available[group] = normalize_probs(forecast.get('probabilities'))
                        weight = (influence.get('weights') or {}).get(group,0.)
                        mixture(baseline,{group:available[group]},{group:weight})
                        weights[group] = weight
                        reason = 'eligible'
                    except ValueError:
                        available.pop(group,None)
                        reason = 'invalid_forecast'
        reasons[group] = reason
    combined = mixture(baseline,available,weights)
    packet = dict(combined, schema_version=1, cutoff=cutoff, symbol=symbol,
                  target=TARGET,cost_band=COST_BAND,baseline_probs=baseline,
                  return_currency=return_currency,label_end=cutoff+86400,
                  coverage=coverage,influence_status=reasons,group_caps=CAPS,
                  evidence=evidence, groups=[{'group':g,'probabilities':p,'weight':combined['weights'][g],'status':reasons[g]}
                                             for g,p in sorted(available.items())],
                  fitted_max_label_end=influence.get('max_label_end') if _finite(influence.get('max_label_end')) else None,
                  calibration_calendar_days=influence.get('calibration_calendar_days',0) if _finite(influence.get('calibration_calendar_days',0)) else 0,
                  live_enabled=False,mode='shadow',promotion_status='research_only')
    # Only canonical finite values enter the hash; ineligible raw data are excluded.
    packet['packet_sha256'] = hashlib.sha256(json.dumps(packet,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return packet
