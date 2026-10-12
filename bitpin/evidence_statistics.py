"""Offline proper scores and capped forecast combination, with calendar resampling.

No account, model, network or execution dependencies. Bootstrap replication is
uncertainty estimation, never additional independent observations.
"""
import math
import random


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('finite numeric value required')
    return float(value)


def normalize_probs(values):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError('three probabilities required')
    result = [_number(v) for v in values]
    if any(v < 0 or v > 1 for v in result) or abs(sum(result) - 1) > 1e-6:
        raise ValueError('probabilities must be nonnegative and sum to one')
    total = sum(result)
    return [v / total for v in result]


def _label(value):
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
        raise ValueError('label must be 0, 1 or 2')
    return value


def score_predictions(rows):
    confusion = [[0]*3 for _ in range(3)]
    losses, loglosses = [], []
    for row in rows:
        label = _label(row['label'])
        probs = normalize_probs(row.get('probabilities', row.get('probs')))
        predicted = max(range(3), key=lambda i: probs[i])
        confusion[label][predicted] += 1
        losses.append(sum((p - int(i == label))**2 for i, p in enumerate(probs)))
        loglosses.append(-math.log(max(probs[label], 1e-12)))
    count = len(losses)
    return {'count': count, 'accuracy': sum(confusion[i][i] for i in range(3))/count if count else None,
            'brier': sum(losses)/count if count else None,
            'logloss': sum(loglosses)/count if count else None,
            'confusion_matrix': confusion, 'class_counts': [sum(r) for r in confusion]}


def fit_influences(rows, groups, caps, cutoff=None, shrinkage=.05):
    """Minimize mean multiclass Brier + L2 penalty by capped coordinate descent.

    Missing group forecasts contribute a zero difference from the baseline.
    The base probabilities must themselves be fitted using older observations.
    The caller supplies the identical event/label for all component forecasts.
    """
    groups = list(groups)
    if len(set(groups)) != len(groups) or _number(shrinkage) < 0:
        raise ValueError('invalid groups or shrinkage')
    limits = {g: _number(caps[g]) for g in groups}
    if any(v < 0 or v > 1 for v in limits.values()):
        raise ValueError('invalid cap')
    if cutoff is not None:
        cutoff = _number(cutoff)
    prepared = []
    for row in rows:
        ts = _number(row.get('time', row.get('decision_time')))
        end = _number(row['label_end'])
        if end <= ts:
            raise ValueError('outcome must end after decision')
        if cutoff is not None and end > cutoff:
            continue
        label = _label(row['label'])
        base = normalize_probs(row['base_probs'])
        forecasts = row.get('forecasts') or {}
        differences = {g: [p-b for p,b in zip(normalize_probs(forecasts[g]), base)]
                       if g in forecasts else [0., 0., 0.] for g in groups}
        prepared.append((ts, end, [b-int(i == label) for i,b in enumerate(base)], differences))
    weights = {g: 0. for g in groups}
    residuals = [list(row[2]) for row in prepared]
    count = len(prepared)
    if count:
        for _ in range(150):
            change = 0.
            for group in groups:
                old = weights[group]
                denom = shrinkage
                numerator = 0.
                for row, residual in zip(prepared, residuals):
                    delta = row[3][group]
                    denom += sum(d*d for d in delta)/count
                    numerator += sum(d*(r-old*d) for d,r in zip(delta,residual))/count
                room = min(limits[group], max(0., 1-sum(w for g,w in weights.items() if g != group)))
                new = max(0., min(room, -numerator/denom)) if denom else 0.
                weights[group] = new
                change = max(change, abs(new-old))
                for row, residual in zip(prepared, residuals):
                    for i,d in enumerate(row[3][group]):
                        residual[i] += (new-old)*d
            if change < 1e-10:
                break
    days = sorted(set(int(r[0]//86400) for r in prepared))
    return {'weights': weights, 'baseline_weight': max(0., 1-sum(weights.values())),
            'sample_count': count, 'calibration_calendar_days': len(days),
            'max_label_end': max((r[1] for r in prepared), default=None),
            'method': 'capped_nonnegative_brier_l2', 'shrinkage': shrinkage}


def _calendar(rows, vector=False):
    result = []
    for i,row in enumerate(rows):
        if isinstance(row, dict):
            ts = _number(row.get('time', row.get('decision_time')))
            values = row['differences'] if vector else row['difference']
        else:
            ts, values = i*86400., row
        if vector:
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError('nonempty candidate vector required')
            values = [_number(v) for v in values]
        else:
            values = [_number(values)]
        result.append((ts, values))
    result.sort(key=lambda r: r[0])
    if len({r[0] for r in result}) != len(result):
        raise ValueError('duplicate calendar dates')
    if result and any(len(r[1]) != len(result[0][1]) for r in result):
        raise ValueError('candidate width mismatch')
    if any(b[0]-a[0] < 86399 for a,b in zip(result,result[1:])):
        raise ValueError('aggregate correlated assets into one calendar day first')
    return result


def _resample_indices(rows, block_size, rng):
    """Stationary bootstrap; a calendar gap forces a new independent block."""
    size = len(rows)
    current = rng.randrange(size)
    result = []
    for _ in range(size):
        result.append(current)
        following = current+1
        contiguous = following < size and abs(rows[following][0]-rows[current][0]-86400) < 1
        if not contiguous or rng.random() < 1./block_size:
            current = rng.randrange(size)
        else:
            current = following
    return result


def _metadata(rows, block_size, repeats):
    if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < 1:
        raise ValueError('positive integer block size required')
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1 or repeats > 10000:
        raise ValueError('repeats must be 1..10000')
    segments = 1 + sum(abs(b[0]-a[0]-86400) >= 1 for a,b in zip(rows,rows[1:])) if rows else 0
    missing = max(0, int(round((rows[-1][0]-rows[0][0])/86400))+1-len(rows)) if rows else 0
    return {'calendar_days': len(rows), 'segments': segments, 'missing_calendar_days': missing,
            'block_size': block_size, 'repeats': repeats, 'method': 'gap_aware_stationary_bootstrap',
            'status': 'diagnostic' if len(rows) >= 30 else 'inconclusive_small_sample'}


def _quantile(values, fraction):
    ordered = sorted(values)
    location = (len(ordered)-1)*fraction
    lo = int(location)
    hi = min(lo+1, len(ordered)-1)
    return ordered[lo] + (ordered[hi]-ordered[lo])*(location-lo)


def paired_block_bootstrap(daily_differences, block_size=7, repeats=500, seed=1729):
    rows = _calendar(daily_differences)
    result = _metadata(rows, block_size, repeats)
    result.update({'mean': sum(r[1][0] for r in rows)/len(rows) if rows else None, 'low': None, 'high': None})
    if len(rows) < 30:
        return result
    rng = random.Random(seed)
    means = [sum(rows[i][1][0] for i in _resample_indices(rows,block_size,rng))/len(rows) for _ in range(repeats)]
    result.update({'low': _quantile(means,.025), 'high': _quantile(means,.975)})
    return result


def reality_check(daily_candidate_differences, block_size=7, repeats=500, seed=1730):
    """Centered White-style family diagnostic, not a proof under nonstationarity."""
    rows = _calendar(daily_candidate_differences, vector=True)
    result = _metadata(rows, block_size, repeats)
    width = len(rows[0][1]) if rows else 0
    means = [sum(r[1][j] for r in rows)/len(rows) for j in range(width)]
    observed = max([0.]+means)
    result.update({'max_mean': observed if rows else None, 'p_value': None, 'candidates': width,
                   'candidate_means': means})
    if len(rows) < 30:
        return result
    rng, exceed = random.Random(seed), 0
    for _ in range(repeats):
        indexes = _resample_indices(rows,block_size,rng)
        maximum = max([0.]+[sum(rows[i][1][j]-means[j] for i in indexes)/len(rows) for j in range(width)])
        exceed += maximum >= observed-1e-14
    result['p_value'] = (exceed+1.)/(repeats+1.)
    return result
