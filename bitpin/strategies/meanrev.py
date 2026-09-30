"""Mean-reversion family: buy deep, fast crashes in liquid coins, otherwise hold USDT_IRT.

Design notes (why it looks like this)
-------------------------------------
* The toman devalues ~4%/month, so the idle ("park") asset is USDT_IRT, not IRT cash. Between
  trades every strategy here is exactly `hold_usdt`; the coin trades are an overlay on top.
* Every coin price is measured in USDT terms (ratio = coin_IRT / USDT_IRT). A coin that "dips"
  only because the toman strengthened is not cheap relative to the USDT we sell to buy it, so
  all oversold logic runs on that ratio.
* Switching USDT -> coin -> USDT costs four taker fills (~1.6% incl. slippage), a FIXED % cost.
  Only dips that are large in % terms revert by more than that, so the main strategy
  (meanrev_dip) uses an absolute % drawdown threshold. Volatility-normalised signals
  (meanrev_zdip, meanrev_rsi) also fire on small % moves of calm coins and did worse on TRAIN.
* What works on TRAIN is SPEED + DEPTH: a >= ~17% fall within ~2 days (liquidation cascades,
  flash crashes, panic). Slow declines of the same size (e.g. 20% over 14 days) did not revert.
* Researched and rejected on TRAIN (kept out to keep the code small): a long-term uptrend
  filter (blocked most of the good crash entries), a market-breadth filter (idiosyncratic
  crashes reverted too), adding weaker alts (SUI/NEAR/ARB/DASH/PEPE: more losers and fees).
* Weights follow a virtual buy-and-hold book: after entry a position's target weight drifts
  with its price instead of being pinned to 1/slots, so the engine never pays fees to
  re-balance an open position; it only trades on entries and exits.
* 4h bars: decisions every 4 hours, fewer reactions to single 1h flash prints.
"""
import math
from collections import deque

from ..backtest import Strategy
from ..indicators import rsi, sma

PARK = "USDT_IRT"
# Large, established coins (global top-cap names) with >= 850 days of IRT history on Bitpin.
# PEPE is excluded on purpose: its 2025-10-10 20:00 1h bar printed -99.7% (a bad tick).
MAJORS = ["BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT", "DOGE_IRT", "BNB_IRT", "ADA_IRT",
          "LINK_IRT", "TRX_IRT"]


# ----------------------------------------------------------------------------- helpers

def _ratio(panel, sym):
    """Coin price in USDT terms (None where either leg is missing)."""
    c = panel[sym]["close"]
    u = panel[PARK]["close"]
    return [None if (a is None or b is None or b <= 0) else a / b for a, b in zip(c, u)]


def _fill(xs):
    """Indicator input without None: forward-fill, and back-fill the leading gap with the first
    valid value. Signals are always masked where the raw series is None."""
    first = next((x for x in xs if x is not None), None)
    if first is None:
        return None
    out, last = [], first
    for x in xs:
        if x is not None:
            last = x
        out.append(last)
    return out


def _rolling_max(xs, n):
    """O(T) causal rolling max over the last n values (monotonic deque); None in warm-up."""
    out = [None] * len(xs)
    dq = deque()
    for i, x in enumerate(xs):
        while dq and xs[dq[-1]] <= x:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = xs[dq[0]]
    return out


def _rolling_std(xs, n):
    """O(T) causal rolling population stdev over the last n values; None in warm-up."""
    out = [None] * len(xs)
    s = s2 = 0.0
    for i, x in enumerate(xs):
        s += x
        s2 += x * x
        if i >= n:
            y = xs[i - n]
            s -= y
            s2 -= y * y
        if i >= n - 1:
            m = s / n
            out[i] = math.sqrt(max(s2 / n - m * m, 0.0))
    return out


def _coins(strategy, panel):
    return [s for s in strategy.symbols if s != PARK and s in panel.cols]


def _book(panel, coins, entry, score, exit_now, slots, max_hold, stop):
    """Sequential virtual portfolio -> causal target weights {symbol: [w_0..w_{T-1}]}.

    entry[s][i]  True if coin s is a buy candidate at the close of bar i.
    score[s][i]  ranking when there are more candidates than free slots (lower = more oversold).
    exit_now(s, i, entry_i) -> True to close an open position at the close of bar i.
    A position is also closed after `max_hold` bars, or when its USDT-terms price is more than
    `stop` below the entry (catastrophe stop; <= 0 disables). Each new position takes 1/slots
    of current equity out of the USDT park; everything not in coins stays in USDT_IRT.
    Entries are edge-triggered: after an exit, coin s can be bought again only once its entry
    condition has been False at least once, so a time/stop exit is not undone on the same dip.
    """
    T = len(panel)
    u = panel[PARK]["close"]
    closes = {s: panel[s]["close"] for s in coins}
    ratio = {s: _ratio(panel, s) for s in coins}
    w = {s: [0.0] * T for s in coins}
    w[PARK] = [0.0] * T
    park = None                 # virtual USDT units (book starts with 1 IRT of equity)
    pos = {}                    # coin -> [units, entry_index, entry_ratio]
    armed = {s: True for s in coins}
    for i in range(T):
        for s in coins:
            if not entry[s][i]:
                armed[s] = True
        if u[i] is None:        # no USDT market yet: stay in IRT cash
            continue
        if park is None:
            park = 1.0 / u[i]
        # 1) exits (decided on the close of bar i; the engine sells at the next open)
        for s in list(pos):
            units, ei, er = pos[s]
            c, r = closes[s][i], ratio[s][i]
            if c is None:
                continue
            if (i - ei >= max_hold or (stop > 0 and r is not None and r < er * (1 - stop))
                    or exit_now(s, i, ei)):
                park += units * c / u[i]
                del pos[s]
                armed[s] = False
        # 2) entries, most oversold first
        if len(pos) < slots:
            cands = [s for s in coins
                     if s not in pos and armed[s] and closes[s][i] is not None and entry[s][i]]
            cands.sort(key=lambda s: score[s][i])
            equity = park * u[i] + sum(p[0] * closes[s][i] for s, p in pos.items())
            for s in cands[: slots - len(pos)]:
                alloc = min(equity / slots, park * u[i])
                if alloc <= 0:
                    break
                park -= alloc / u[i]
                pos[s] = [alloc / closes[s][i], i, ratio[s][i]]
        # 3) target weights = value shares of the virtual book
        vals = {s: p[0] * closes[s][i] for s, p in pos.items()}
        eq = park * u[i] + sum(vals.values())
        for s, v in vals.items():
            w[s][i] = v / eq
        w[PARK][i] = park * u[i] / eq
    return w


def _sma_exit(panel, coins, n):
    """exit_now callback: close once the USDT-terms price is back above its n-bar SMA."""
    ratios = {s: _ratio(panel, s) for s in coins}
    mids = {}
    for s in coins:
        f = _fill(ratios[s])
        mids[s] = sma(f, n) if f is not None else [None] * len(panel)

    def exit_now(s, i, ei):
        m, r = mids[s][i], ratios[s][i]
        return i > ei and m is not None and r is not None and r >= m
    return exit_now


# ----------------------------------------------------------------------------- strategies

class MeanRevDip(Strategy):
    """Fast-crash buyer (main strategy of the family).

    Entry : USDT-terms price is >= dd_entry below its highest close of the last dd_lb bars
            (defaults: 17.5% below the 2-day high), edge-triggered, at most `slots` positions.
    Exit  : price back above its exit_n-bar SMA (default 7 days), or max_hold bars (14 days),
            or a catastrophe stop 20% below entry.
    TRAIN plateau: dd_lb 9..15 bars with dd_entry 0.15..0.20 all beat hold_usdt on win30
    median and p10; thresholds shallower than ~0.13 at this speed let too many ordinary dips
    through and the fees eat the edge.
    """
    name = "meanrev_dip"
    res = "240"
    symbols = MAJORS + [PARK]
    description = ("Buys liquid IRT majors after a >=17.5% fall within 2 days (USDT terms), sells on "
                   "the rebound to the 7-day mean; parks in USDT_IRT. ~1 round trip per 2-3 weeks.")
    default_params = {"dd_lb": 12, "dd_entry": 0.175, "exit_n": 42, "max_hold": 84, "stop": 0.2,
                      "slots": 3}
    param_grid = {"dd_lb": [6, 12, 18], "dd_entry": [0.125, 0.15, 0.175, 0.2, 0.225],
                  "exit_n": [18, 42, 84], "slots": [2, 3, 4]}

    def weights(self, panel):
        p = self.params
        coins = _coins(self, panel)
        T = len(panel)
        entry, score = {}, {}
        for s in coins:
            raw = _ratio(panel, s)
            r = _fill(raw)
            e, sc = [False] * T, [0.0] * T
            if r is not None:
                hi = _rolling_max(r, p["dd_lb"])
                for i in range(T):
                    if raw[i] is None or hi[i] is None:
                        continue
                    sc[i] = r[i] / hi[i] - 1          # drawdown from the recent high (<= 0)
                    e[i] = sc[i] <= -p["dd_entry"]
            entry[s], score[s] = e, sc
        return _book(panel, coins, entry, score, _sma_exit(panel, coins, p["exit_n"]),
                     p["slots"], p["max_hold"], p["stop"])


class MeanRevZDip(Strategy):
    """Volatility-scaled variant of meanrev_dip: the crash threshold is in units of each coin's
    own recent volatility, so BTC qualifies on a smaller % fall than DOGE.

    z = ln(price / max(price over dd_lb bars)) / (sigma * sqrt(dd_lb)), sigma = stdev of 4h log
    returns over vol_n bars ending BEFORE the dip window (the crash must not inflate its own
    yardstick). Entry when z <= -z_entry; exits as meanrev_dip.
    """
    name = "meanrev_zdip"
    res = "240"
    symbols = MAJORS + [PARK]
    description = ("Volatility-scaled fast-crash buyer on liquid IRT majors (USDT terms); parks in "
                   "USDT_IRT.")
    default_params = {"dd_lb": 12, "z_entry": 3.5, "vol_n": 180, "exit_n": 42, "max_hold": 84,
                      "stop": 0.2, "slots": 3}
    param_grid = {"dd_lb": [6, 12, 18], "z_entry": [2.5, 3.0, 3.5, 4.0], "vol_n": [90, 180],
                  "exit_n": [18, 42]}

    def weights(self, panel):
        p = self.params
        coins = _coins(self, panel)
        T = len(panel)
        L = p["dd_lb"]
        entry, score = {}, {}
        for s in coins:
            raw = _ratio(panel, s)
            r = _fill(raw)
            e, sc = [False] * T, [0.0] * T
            if r is not None:
                lr = [0.0] + [math.log(r[i] / r[i - 1]) for i in range(1, T)]
                sd = _rolling_std(lr, p["vol_n"])
                hi = _rolling_max(r, L)
                for i in range(L, T):
                    v = sd[i - L]                     # yardstick from before the dip window
                    if raw[i] is None or hi[i] is None or not v:
                        continue
                    sc[i] = math.log(r[i] / hi[i]) / (v * math.sqrt(L))
                    e[i] = sc[i] <= -p["z_entry"]
            entry[s], score[s] = e, sc
        return _book(panel, coins, entry, score, _sma_exit(panel, coins, p["exit_n"]),
                     p["slots"], p["max_hold"], p["stop"])


class MeanRevRSI(Strategy):
    """Textbook RSI swing on the USDT-terms price (4h bars), kept as the family's reference.

    Entry : RSI(rsi_n) < entry_rsi.   Exit : RSI > exit_rsi, max_hold bars, or stop.
    Weaker and much more parameter-sensitive than meanrev_dip on TRAIN (RSI is volatility-
    normalised, so it also buys small % dips that cannot pay the ~1.6% switching cost).
    """
    name = "meanrev_rsi"
    res = "240"
    symbols = MAJORS + [PARK]
    description = "RSI(4h) oversold buyer on liquid IRT majors in USDT terms; parks in USDT_IRT."
    default_params = {"rsi_n": 14, "entry_rsi": 15, "exit_rsi": 65, "max_hold": 84, "stop": 0.2,
                      "slots": 3}
    param_grid = {"rsi_n": [7, 14, 28], "entry_rsi": [10, 15, 20], "exit_rsi": [50, 65],
                  "max_hold": [42, 84], "slots": [2, 3]}

    def weights(self, panel):
        p = self.params
        coins = _coins(self, panel)
        T = len(panel)
        entry, score, rs = {}, {}, {}
        for s in coins:
            raw = _ratio(panel, s)
            r = _fill(raw)
            x = rsi(r, p["rsi_n"]) if r is not None else [None] * T
            rs[s] = x
            entry[s] = [raw[i] is not None and x[i] is not None and x[i] < p["entry_rsi"]
                        for i in range(T)]
            score[s] = [x[i] if x[i] is not None else 100.0 for i in range(T)]

        def exit_now(s, i, ei):
            return rs[s][i] is not None and rs[s][i] > p["exit_rsi"]

        return _book(panel, coins, entry, score, exit_now, p["slots"], p["max_hold"], p["stop"])
