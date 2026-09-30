"""Shared research harness so every strategy is judged the same way.

Data split (fixed, do not change):
  TRAIN   : first bar .. SPLIT_TS          (tune parameters here only)
  HOLDOUT : SPLIT_TS .. last bar (150 days) (touched only by the verification stage)

All reports include benchmarks: hold cash (IRT), hold USDT (USDT_IRT buy & hold) and equal-weight
buy & hold of the strategy's own symbols, plus the result expressed in USDT terms.
"""
import importlib
import itertools
import os
import pkgutil
import sys

from .backtest import Panel, Strategy, buy_and_hold, check_no_lookahead, evaluate, metrics, simulate

SPLIT_TS = 1777105800          # 2026-04-25 08:30 UTC
HOLDOUT_DAYS = 150
DEFAULT_SIM = {"fee": 0.0035, "slippage": 0.0005, "rebalance_threshold": 0.02}


def discover_strategies():
    """Return {name: class} for every Strategy subclass in bitpin/strategies/*.py."""
    from . import strategies as pkg
    found = {}
    for m in pkgutil.iter_modules([os.path.dirname(pkg.__file__)]):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module("bitpin.strategies." + m.name)
        except Exception as e:  # noqa: BLE001 - a broken module must not break the others
            print("warning: cannot import strategy module %s: %s" % (m.name, e), file=sys.stderr)
            continue
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == mod.__name__:
                found[obj.name] = obj
    return found


def load_panel_for(strategy, extra=("USDT_IRT",)):
    syms = list(dict.fromkeys(list(strategy.symbols) + [s for s in extra if s not in strategy.symbols]))
    return Panel.load(syms, strategy.res)


def usdt_terms(result, panel):
    """Convert an IRT equity curve into USDT using USDT_IRT closes."""
    if "USDT_IRT" not in panel.cols:
        return None
    c = panel["USDT_IRT"]["close"]
    start = len(panel) - len(result["equity"])
    eq = [e / c[start + i] for i, e in enumerate(result["equity"])]
    r = dict(result)
    r["equity"] = eq
    r["init"] = eq[0]
    r["fees"] = result["fees"] / c[start]
    return r


def _round(d):
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def report(strategy, split="train", sim=None, lookahead_samples=8):
    """Full evaluation of one parameterised strategy on 'train' or 'holdout'."""
    sim = dict(DEFAULT_SIM, **(sim or {}))
    panel = load_panel_for(strategy)
    if split == "train":
        start_ts, end_ts = None, SPLIT_TS
        warm_start = panel.ts[0] + 30 * 86400  # skip first 30 days so indicators are warm
        start_ts = warm_start
    elif split == "holdout":
        start_ts, end_ts = SPLIT_TS, None
    elif split == "all":
        start_ts, end_ts = panel.ts[0] + 30 * 86400, None
    else:
        raise ValueError(split)
    m, res = evaluate(strategy, panel, start_ts, end_ts, **sim)
    quote_is_irt = all(s.endswith("_IRT") for s in strategy.symbols)
    out = {"strategy": repr(strategy), "split": split, "irt": _round(m)}
    if quote_is_irt:
        end_i = panel.index_at_or_after(end_ts) if end_ts else len(panel)
        ut = usdt_terms(res, panel.slice(0, end_i))
        if ut:
            out["usdt_terms"] = _round(metrics(ut, panel.res))
        bh_usdt, _ = buy_and_hold(panel, ["USDT_IRT"], start_ts, end_ts, **sim)
        out["bench_hold_usdt"] = _round(bh_usdt)
    bh_own, _ = buy_and_hold(panel, list(strategy.symbols), start_ts, end_ts, **sim)
    out["bench_hold_own_eqw"] = _round(bh_own)
    if lookahead_samples:
        sub = panel.slice(0, panel.index_at_or_after(end_ts)) if end_ts else panel
        out["lookahead_violations"] = check_no_lookahead(strategy, sub, samples=lookahead_samples)[:5]
    out["trades_sample"] = [list(t) for t in res["trades"][-3:]]
    return out


def sensitivity(strategy_cls, params, split="train", scale=(0.75, 1.25), sim=None):
    """Perturb each numeric parameter by the given factors; report key metrics per variant."""
    rows = []
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        for f in scale:
            nv = type(v)(round(v * f)) if isinstance(v, int) else v * f
            if nv == v:
                continue
            p = dict(params)
            p[k] = nv
            r = report(strategy_cls(**p), split, sim, lookahead_samples=0)
            rows.append({"param": k, "value": nv, "total_return": r["irt"]["total_return"],
                         "max_drawdown": r["irt"]["max_drawdown"], "win30_median": r["irt"]["win30_median"],
                         "win30_p10": r["irt"]["win30_p10"]})
    return rows


def grid(strategy_cls, split="train", max_configs=200, sim=None, key="win30_median"):
    """Evaluate the strategy's param_grid on TRAIN. Returns rows sorted by `key` (desc)."""
    g = strategy_cls.param_grid or {}
    names = list(g)
    combos = list(itertools.product(*[g[n] for n in names]))[:max_configs]
    rows = []
    for c in combos:
        p = dict(zip(names, c))
        try:
            r = report(strategy_cls(**p), split, sim, lookahead_samples=0)
        except Exception as e:  # noqa: BLE001
            rows.append({"params": p, "error": str(e)})
            continue
        rows.append({"params": p, **{k: r["irt"].get(k) for k in
                                     ("total_return", "max_drawdown", "sharpe", "win30_median", "win30_p10",
                                      "win30_positive_frac", "trades", "fees_frac")}})
    rows.sort(key=lambda r: -(r.get(key) if r.get(key) is not None else -1e9))
    return rows
