"""The strategy the bot runs by default for the next 30 days: the hold_usdt BASELINE.

Why the baseline (decision of 2026-09-22)
-----------------------------------------
Five research families were built and independently verified: trend, rotation, meanrev, hedge and
relval, with 15 candidates in total. Every candidate beat hold_usdt on TRAIN, but every one was
REJECTED on the untouched 150-day HOLDOUT (2026-04-25 .. 2026-09-22). None beat hold_usdt on the
30-day p10 and none had a lower max drawdown, except as a single-trade fluke (meanrev_dip dd_lb=6)
or by never trading (relval gold, whose holdout result is identical to hold_usdt). Several had a
higher 30-day median but a much worse left tail: p10 of -7% to -14% against -4.6% for hold_usdt.
For someone running the bot for one month, that is a worse deal.

The house rule is: when no candidate is accepted, run the baseline. So "selected" is hold_usdt.
It converts the toman once into USDT_IRT and holds it. That is one trade (fee 0.35%) and after
that nothing, which protects against toman devaluation (about +3.9%/month on TRAIN).
It does NOT protect against toman-rally months: USDT_IRT itself fell about 22% in Mar-Apr 2025
and about 15% in Feb 2026.

To switch strategy later, change the class this one inherits from and its default_params, then
re-run `py scripts/research_eval.py selected --split train` and `--split holdout`.
"""
from .hold_usdt import HoldUSDT


class Selected(HoldUSDT):
    name = "selected"
    res = HoldUSDT.res
    symbols = list(HoldUSDT.symbols)
    default_params = dict(HoldUSDT.default_params)
    param_grid = {}
    description = ("Default bot strategy = hold_usdt baseline (buy USDT_IRT once and hold). "
                   "No researched strategy survived the holdout, so the baseline is used.")
