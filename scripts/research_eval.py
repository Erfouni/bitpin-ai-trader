"""Evaluate a strategy with the shared harness and print JSON.

  py scripts/research_eval.py hold_usdt
  py scripts/research_eval.py trend_donchian --params "{\"entry\": 55}" --split train
  py scripts/research_eval.py trend_donchian --grid            (TRAIN grid search over param_grid)
  py scripts/research_eval.py trend_donchian --sensitivity --params "{...}"
  py scripts/research_eval.py trend_donchian --split holdout --params "{...}"   (verification only)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitpin.research import discover_strategies, grid, report, sensitivity  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy")
    ap.add_argument("--params", default="{}")
    ap.add_argument("--split", default="train", choices=["train", "holdout", "all"])
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--sensitivity", action="store_true")
    ap.add_argument("--max-configs", type=int, default=200)
    ap.add_argument("--sim", default="{}", help='override sim, e.g. {"slippage": 0.002}')
    a = ap.parse_args()
    strategies = discover_strategies()
    if a.strategy not in strategies:
        sys.exit("unknown strategy %r; available: %s" % (a.strategy, sorted(strategies)))
    cls = strategies[a.strategy]
    params = json.loads(a.params)
    sim = json.loads(a.sim)
    if a.grid:
        if a.split != "train":
            sys.exit("grid search is only allowed on the train split")
        out = grid(cls, "train", a.max_configs, sim)
    elif a.sensitivity:
        out = sensitivity(cls, dict(cls.default_params, **params), a.split, sim=sim)
    else:
        out = report(cls(**params), a.split, sim)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
