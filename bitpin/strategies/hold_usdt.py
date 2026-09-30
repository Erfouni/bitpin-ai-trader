"""Baseline: convert all toman to USDT once and hold. Every IRT strategy must beat this."""
from ..backtest import Strategy


class HoldUSDT(Strategy):
    name = "hold_usdt"
    res = "60"
    symbols = ["USDT_IRT"]
    description = "Buy USDT with all IRT at the start and hold (toman-devaluation hedge baseline)."
    default_params = {}

    def weights(self, panel):
        c = panel["USDT_IRT"]["close"]
        return {"USDT_IRT": [1.0 if x is not None else 0.0 for x in c]}
