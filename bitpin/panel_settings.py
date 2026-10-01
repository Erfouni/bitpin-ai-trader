# -*- coding: utf-8 -*-
"""The trade settings form of the management panel (v3.1).

One table (FIELDS) of the config.json / kimi.json keys the owner tunes most - Kimi's style, risk, the
decision schedule and wake-ups, markets, the crash ladder, exits and routing, costs, news and the model
budget - each with a label and a short help text in Persian and English (v3.2: Text pairs, bitpin/panel_i18n)
and an input kind. Both sides of the panel use it:

* the web panel (bitpin/panel_web.py, unprivileged) renders the form from it (form_values) and turns
  the posted form into a list of changes (parse_form);
* the root helper (scripts/panel_helper.py) accepts ONLY changes of these keys: apply_changes checks
  the key, the kind and the bounds again (the web side is not trusted), and the helper then runs the
  full validation (deploy/apply_profile.validate: runner.load_config, RiskManager,
  brain.check_kimi_config) before anything is written.

Every other key stays editable in the JSON editors of the settings page. Nothing here reads secrets or
writes files; the functions take and return parsed JSON documents (help_en reads the example files).
"""
import copy
import json
import os
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from .panel_i18n import Text as T

MISSING = object()

GROUPS = [
    ("style", T("سبک معامله و دستور کیمی", "Trading style and Kimi's brief"),
     T("کیمی تصمیم می‌گیرد؛ این‌ها چارچوب و سبک تصمیم او را تعیین می‌کنند.",
       "Kimi decides; these set the frame and the style of its decisions.")),
    ("risk", T("ریسک و حد توقف", "Risk and the drawdown halt"),
     T("محافظ‌های کد که همیشه اجرا می‌شوند، هر چه کیمی بگوید.",
       "Guards in the code that always apply, whatever Kimi says.")),
    ("schedule", T("زمان تصمیم‌ها و بیدارباش‌ها", "Decision times and wake-ups"),
     T("هر تصمیم یک تماس با مدل است و هزینه دارد.", "Every decision is a model call and costs money.")),
    ("markets", T("بازارها و محافظ پامپ", "Markets and the pump guard"),
     T("نام بازارها را با ویرگول جدا کنید، مثل BTC_IRT, ETH_IRT.",
       "Separate market names with commas, e.g. BTC_IRT, ETH_IRT.")),
    ("ladder", T("نردبان خرید در ریزش", "Dip-buying ladder"),
     T("سفارش‌های خرید لیمیت که زیر سقف ۴۸ ساعته روی بیت‌پین منتظر ریزش می‌مانند.",
       "Limit buy orders that wait on Bitpin below the 48-hour high for a dip.")),
    ("exits", T("خروج‌ها، مسیر معامله و حالت پشتیبان", "Exits, order routing and the fallback"), None),
    ("costs", T("کارمزد و آستانه‌ها", "Fees and thresholds"),
     T("کارمزد بیت‌پین: میکر ۰٫۳۰٪، تیکر ۰٫۳۵٪. معمولاً لازم نیست تغییر کند.",
       "Bitpin's fees: maker 0.30%, taker 0.35%. Usually nothing to change here.")),
    ("news", T("اخبار", "News"),
     T("مرحله‌ی خبر پیش از تصمیم روزانه: ربات تیترهای تازه‌ی منابع معتبر را از فیدشان می‌خواند و مدل خبر (صفحه‌ی "
       "مدل‌ها و کلیدها) خبرهای مهم را انتخاب و خلاصه می‌کند.",
       "The news step before the daily decision: the bot reads the latest headlines of the trusted sources from their "
       "feeds and the news model (Models & keys) picks and summarises the important events.")),
    ("budget", T("بودجه‌ی مدل", "Model budget"),
     T("سقف‌های روزانه‌ی تماس و توکن: محافظ هزینه.", "Daily caps on calls and tokens: the cost guard.")),
]

KINDS = ("int", "float", "pct", "bool", "choice", "text", "longtext", "symbols", "coins", "coins_or_null",
         "numbers", "times", "domains", "urls")
LIST_KINDS = ("symbols", "coins", "coins_or_null", "numbers", "times", "domains", "urls")


class Field(object):
    """One form input. key = "<file>:<dotted path>"; label, help and unit are Text (Persian, English) pairs;
    bounds (lo, hi) are in DISPLAY units (a "pct" field stores a fraction, 0.5, and is shown and bounded as a
    percent, 50)."""

    def __init__(self, file, path, kind, group, label, help=None, unit="", lo=None, hi=None, choices=None,
                 nullable=False, max_len=None, min_items=None, max_items=None):
        assert file in ("config", "kimi") and kind in KINDS, (file, kind)
        assert isinstance(label, T) and (help is None or isinstance(help, T)), (file, path)
        self.file = file
        self.path = tuple(path.split("."))
        self.key = "%s:%s" % (file, path)
        self.kind = kind
        self.group = group
        self.label = label
        self.help = help
        self.unit = unit
        self.lo = lo
        self.hi = hi
        self.choices = list(choices or [])
        self.nullable = nullable
        self.max_len = max_len
        self.min_items = min_items
        self.max_items = max_items

    @property
    def dotted(self):
        return ".".join(self.path)

    def __repr__(self):
        return "Field(%s)" % self.key


P = T("٪", "%")
DAYS = T("روز", "days")
HOURS = T("ساعت", "hours")
MINUTES = T("دقیقه", "minutes")
IRT = T("تومان", "IRT")
OF_EQUITY = T("% از کل حساب", "% of equity")
POINTS = T("واحد درصد", "percentage points")
AGAIN_ABOVE = T("وقتی کوین به بالای این فاصله از سقف ۴۸ ساعته برگردد.",
                "When the coin climbs back above this distance from its 48-hour high.")
FIELDS = [
    # ------------------------------------------------------------------ style
    Field("kimi", "brain.extra_instructions", "longtext", "style",
          T("دستور سبک معامله برای کیمی", "Trading-style brief for Kimi"),
          T("به دستورهای کیمی اضافه می‌شود. به انگلیسی، حداکثر ۴۰۰۰ نویسه.",
            "Added to Kimi's instructions. In English, up to 4000 characters."), max_len=4000),
    Field("kimi", "brain.risk_profile", "choice", "style", T("سطح اختیار کیمی", "Kimi's latitude"),
          T("full یعنی کیمی کنترل کامل دارد و فقط محافظ‌های فنی کد می‌مانند.",
            "full: Kimi has full control and only the code's technical guards remain."),
          choices=[("full", T("کامل: بدون سقف وزن", "Full: no weight cap")),
                   ("aggressive", T("تهاجمی: هر کوین حداکثر ۵۰٪", "Aggressive: at most 50% per coin")),
                   ("balanced", T("متعادل: هر کوین حداکثر ۳۰٪", "Balanced: at most 30% per coin")),
                   ("conservative", T("محتاط: هر کوین حداکثر ۱۵٪", "Conservative: at most 15% per coin"))]),
    Field("kimi", "brain.analysis_policy", "choice", "style",
          T("بررسی تحلیل کیمی با کد", "The code's check of Kimi's analysis"),
          T("کد حساب احتمال و سود/زیان مورد انتظار کیمی را دوباره می‌سنجد.",
            "The code re-checks Kimi's probability and expected profit / loss arithmetic."),
          choices=[("off", T("فقط یادداشت (خاموش)", "Note only (off)")),
                   ("block", T("افزایش وزنِ کوینِ مردود انجام نشود", "Do not raise the weight of a coin that fails")),
                   ("error", T("کل پاسخ مردود رد شود", "Reject the whole answer when it fails"))]),
    Field("kimi", "brain.require_plan", "bool", "style",
          T("برنامه‌ی ورود برای هر موقعیت تازه لازم است", "Every new position needs an entry plan"),
          T("نوع ستاپ، افق زمانی و قیمت ابطال.", "Setup type, time horizon and invalidation price.")),
    Field("kimi", "brain.slot_reasoning_effort", "choice", "style",
          T("عمق فکر تصمیم روزانه", "Reasoning effort of the daily decision"),
          T("فقط برای مدل‌هایی که عمق فکر دارند (مثل kimi-k3).",
            "Only for models with a reasoning setting (like kimi-k3)."),
          choices=[(None, T("مثل تنظیم مدل", "As the model is set")), ("low", "low"), ("high", "high"),
                   ("max", "max")]),
    # ------------------------------------------------------------------ risk
    Field("config", "risk.max_drawdown", "pct", "risk", T("حد افت برای توقف معامله", "Drawdown that halts trading"),
          T("اگر ارزش حساب این مقدار از سقفِ دوره‌ی اخیر پایین بیاید، ربات معامله را متوقف می‌کند.",
            "If the account falls this far below its recent peak, the bot stops trading."),
          unit=P, lo=1, hi=95),
    Field("config", "risk.hwm_window_days", "int", "risk", T("دوره‌ی سقف ارزش حساب", "Peak window"),
          T("سقف ارزش از این تعداد روز اخیر حساب می‌شود؛ ۰ یعنی از ابتدا.",
            "The peak is taken over this many recent days; 0 = since the start."), unit=DAYS, lo=0, hi=3660),
    Field("config", "risk.drawdown_action", "choice", "risk",
          T("کار ربات هنگام رسیدن به حد افت", "What the bot does at the drawdown limit"),
          choices=[("halt", T("فقط توقف؛ دارایی‌ها نگه داشته شوند", "Halt only; keep the holdings")),
                   ("flatten", T("توقف و فروش همه‌ی کوین‌ها به تومان", "Halt and sell every coin for IRT"))]),
    Field("config", "max_equity_irt", "int", "risk", T("سقف سرمایه‌ی تحت مدیریت", "Capital the bot manages (cap)"),
          T("خالی یعنی کل حساب. با یک عدد، ربات فقط همین مقدار را معامله می‌کند و به کوین‌هایی که الان در حساب هست "
            "دست نمی‌زند.",
            "Empty = the whole account. With a number the bot trades only that much and leaves the coins already in "
            "the account alone."), unit=IRT, lo=1, hi=10 ** 13, nullable=True),
    Field("config", "risk.max_order_fraction", "pct", "risk", T("بزرگ‌ترین سفارش", "Largest order"),
          T("سفارش بزرگ‌تر کوچک می‌شود و بقیه در ساعت بعد اجرا می‌شود.",
            "A larger order is cut down; the rest runs in the next hour."), unit=OF_EQUITY, lo=1, hi=100),
    Field("config", "risk.max_slippage", "pct", "risk", T("حداکثر لغزش قیمت هر سفارش", "Maximum slippage per order"),
          T("سفارشی که دفتر سفارش نمی‌تواند با این لغزش بپذیرد کوچک می‌شود.",
            "An order the book cannot fill within this slippage is made smaller."), unit=P, lo=0.05, hi=20),
    Field("config", "risk.max_price_deviation", "pct", "risk",
          T("حداکثر فاصله‌ی قیمت از آخرین قیمت ساعتی", "Maximum distance from the last hourly price"),
          unit=P, lo=0.5, hi=50),
    Field("config", "risk.min_order_irt", "int", "risk", T("کوچک‌ترین سفارش تومانی", "Smallest IRT order"),
          unit=IRT, lo=0, hi=10 ** 10),
    Field("config", "risk.min_order_usdt", "float", "risk", T("کوچک‌ترین سفارش تتری", "Smallest USDT order"),
          unit="USDT", lo=0, hi=100000),
    Field("config", "risk.max_orders_per_day", "int", "risk", T("سقف سفارش‌ها در ۲۴ ساعت", "Orders per 24 hours (cap)"),
          T("محافظ حلقه‌ی خراب، نه بودجه‌ی معامله.", "A guard against a broken loop, not a trading budget."),
          lo=1, hi=5000),
    Field("config", "risk.max_limit_orders_per_day", "int", "risk",
          T("سقف سفارش‌های لیمیت در ۲۴ ساعت", "Limit orders per 24 hours (cap)"), lo=0, hi=5000),
    Field("config", "risk.max_limit_distance", "pct", "risk",
          T("حداکثر فاصله‌ی سفارش لیمیت از قیمت", "Maximum distance of a limit order from the price"),
          unit=P, lo=1, hi=99),
    # ------------------------------------------------------------------ schedule
    Field("kimi", "brain.decision_times_local", "times", "schedule",
          T("ساعت‌های تصمیم روزانه (تهران)", "Daily decision times (Tehran)"),
          T("مثل 19:00 یا چند ساعت: 13:00, 19:00. خالی یعنی زمان‌بندی قدیمی هر چند ساعت.",
            "Like 19:00, or several: 13:00, 19:00. Empty = the old every-few-hours schedule."),
          min_items=0, max_items=24),
    Field("kimi", "brain.max_gap_hours", "int", "schedule",
          T("حداکثر فاصله‌ی دو تصمیم", "Longest gap between two decisions"), unit=HOURS, lo=24, hi=168),
    Field("kimi", "brain.held_move_pct", "float", "schedule",
          T("بیدارباش با حرکت کوینِ در دست", "Wake-up on a move of a held coin"),
          T("حرکت مثبت یا منفی از آخرین تصمیم، به تتر.", "A move up or down since the last decision, in USDT."),
          unit=P, lo=1, hi=100),
    Field("kimi", "brain.drawdown_trigger_points", "float", "schedule",
          T("بیدارباش با بدتر شدن افت حساب", "Wake-up when the drawdown deepens"), unit=POINTS, lo=0.5, hi=50),
    Field("kimi", "brain.usdt_notify_pct", "float", "schedule",
          T("اعلان حرکت تتر به تومان", "Alert on a USDT / IRT move"),
          T("فقط پیام، بدون تماس با کیمی. خالی = خاموش.", "A message only, no call to Kimi. Empty = off."),
          unit=P, lo=0.5, hi=50, nullable=True),
    Field("kimi", "brain.veto_drop_pct", "float", "schedule",
          T("بیدارباش وتو با ریزش کوین نردبان", "Veto wake-up when a ladder coin drops"),
          T("کیمی می‌تواند سفارش‌های نردبان آن کوین را لغو یا کوچک کند. خالی = خاموش.",
            "Kimi may cancel or shrink that coin's ladder orders. Empty = off."),
          unit=P, lo=1, hi=90, nullable=True),
    Field("kimi", "brain.veto_rearm_pct", "float", "schedule", T("فعال شدن دوباره‌ی وتو", "The veto re-arms"),
          AGAIN_ABOVE, unit=P, lo=0.5, hi=90),
    Field("kimi", "brain.max_decisions_per_day", "int", "schedule",
          T("سقف تصمیم‌های عادی در ۲۴ ساعت", "Regular decisions per 24 hours (cap)"), lo=1, hi=48),
    Field("kimi", "brain.max_early_decisions_per_day", "int", "schedule",
          T("سقف تصمیم‌های زودهنگام در ۲۴ ساعت", "Early decisions per 24 hours (cap)"), lo=0, hi=48),
    Field("kimi", "brain.min_decision_spacing_minutes", "int", "schedule",
          T("حداقل فاصله‌ی دو تماس با مدل", "Shortest gap between two model calls"), unit=MINUTES, lo=0, hi=1440),
    # ------------------------------------------------------------------ markets
    Field("kimi", "brain.allowed_symbols", "symbols", "markets", T("بازارهای مجاز برای خرید", "Markets Kimi may buy"),
          T("کیمی فقط این‌ها را می‌تواند بخرد؛ هر کدام باید در «بازارهای تحلیل‌شده» هم باشد. USDT_IRT لازم است.",
            "Kimi can buy only these; each must also be one of the analysed markets. USDT_IRT is required."),
          min_items=1, max_items=200),
    Field("kimi", "context.universe", "symbols", "markets", T("بازارهای تحلیل‌شده", "Analysed markets"),
          T("کیمی داده‌ی این بازارها را می‌بیند.", "Kimi sees the data of these markets."), min_items=1, max_items=200),
    Field("kimi", "context.max_spread_pct", "float", "markets", T("حداکثر اسپرد برای خرید", "Widest spread to buy at"),
          T("کوینی با اسپرد بیشتر یا دفتر سفارش خالی خریده نمی‌شود.",
            "A coin with a wider spread or an empty order book is not bought."), unit=P, lo=0.05, hi=20),
    Field("kimi", "guard.pump_rise_pct", "float", "markets", T("محافظ پامپ: رشد", "Pump guard: rise"),
          T("کوینی که در بازه‌ی زیر این مقدار بالا رفته خریده نمی‌شود.",
            "A coin that rose this much within the window below is not bought."), unit=P, lo=5, hi=500),
    Field("kimi", "guard.pump_window_hours", "int", "markets", T("محافظ پامپ: بازه‌ی رشد", "Pump guard: rise window"),
          unit=HOURS, lo=1, hi=720),
    Field("kimi", "guard.pump_lookback_hours", "int", "markets", T("محافظ پامپ: مدت منع خرید", "Pump guard: buy ban"),
          unit=HOURS, lo=1, hi=720),
    # ------------------------------------------------------------------ ladder
    Field("config", "ladder.enabled", "bool", "ladder", T("نردبان روشن باشد", "Ladder on"),
          T("خاموش: سفارش‌های نردبان در بررسی ساعت بعد لغو می‌شوند.",
            "Off: the ladder orders are cancelled at the next hourly check.")),
    Field("kimi", "brain.ladder_coins", "coins", "ladder", T("کوین‌های نردبان", "Ladder coins"),
          T("هر کوین باید بازار COIN_USDT و COIN_IRT داشته باشد. (اگر ladder.coins در config.json پر باشد، آن "
            "اولویت دارد.)",
            "Each coin needs a COIN_USDT and a COIN_IRT market. (ladder.coins in config.json wins when it is set.)"),
          min_items=0, max_items=20),
    Field("config", "ladder.levels_pct", "numbers", "ladder", T("سطح‌های خرید", "Buy levels"),
          T("درصد زیر سقف ۴۸ ساعته، مثل -20 یا -20, -25.", "Percent below the 48-hour high, like -20 or -20, -25."),
          unit=P, lo=-90, hi=-1, min_items=1, max_items=6),
    Field("config", "ladder.size_frac", "pct", "ladder", T("اندازه‌ی هر سفارش نردبان", "Size of each ladder order"),
          T("ضربدر مقیاسی که کیمی برای هر کوین می‌دهد؛ فقط از تتر آزاد پرداخت می‌شود.",
            "Times the scale Kimi gives each coin; paid only from free USDT."), unit=OF_EQUITY, lo=0.5, hi=100),
    Field("config", "ladder.rearm_pct", "float", "ladder", T("فعال شدن دوباره‌ی سطحِ پرشده", "A filled level re-arms"),
          AGAIN_ABOVE, unit=P, lo=0.5, hi=50),
    Field("config", "ladder.lookback_hours", "int", "ladder",
          T("سقف قیمت از چند ساعت اخیر", "The high is taken over the last"), unit=HOURS, lo=1, hi=720),
    # ------------------------------------------------------------------ exits
    Field("config", "exits.enabled", "bool", "exits",
          T("حد ضرر، هدف و حداکثر نگهداری با کد اجرا شود", "The code enforces stops, targets and the longest hold"),
          T("سطح‌ها را کیمی برای هر موقعیت تعیین می‌کند.", "Kimi sets the levels of each position.")),
    Field("config", "exits.target_orders", "bool", "exits",
          T("هدف فروش به‌صورت سفارش لیمیت روی بیت‌پین بماند", "Keep each sell target as a limit order on Bitpin"),
          T("خاموش: هدف روی قیمت بسته شدن ساعتی بررسی می‌شود.", "Off: the target is checked against the hourly close.")),
    Field("config", "routing.enabled", "bool", "exits",
          T("معامله از مسیر بازار تتری وقتی ارزان‌تر است", "Trade through the USDT market when it is cheaper")),
    Field("config", "routing.coins", "coins_or_null", "exits", T("کوین‌های مسیر تتری", "Coins routed through USDT"),
          T("خالی یعنی همه‌ی کوین‌هایی که بازار تتری دارند.", "Empty = every coin that has a USDT market."),
          min_items=1, max_items=100),
    Field("kimi", "brain.fallback.derisk_after_hours", "int", "exits",
          T("بدون تصمیم معتبر، انتقال کوین‌ها به تتر بعد از", "Without a valid decision, move coins to USDT after"),
          T("کوین‌هایی که خروج کدی فعال دارند فروخته نمی‌شوند.", "Coins with an active code exit are not sold."),
          unit=HOURS, lo=1, hi=720),
    # ------------------------------------------------------------------ costs
    Field("config", "fee_rate", "pct", "costs", T("کارمزد تیکر برای برنامه‌ریزی", "Taker fee used for planning"),
          unit=P, lo=0, hi=5),
    Field("kimi", "brain.maker_fee", "pct", "costs", T("کارمزد میکر (برای کیمی)", "Maker fee (told to Kimi)"),
          unit=P, lo=0, hi=5),
    Field("kimi", "brain.taker_fee", "pct", "costs", T("کارمزد تیکر (برای کیمی)", "Taker fee (told to Kimi)"),
          unit=P, lo=0, hi=5),
    Field("config", "rebalance_threshold", "pct", "costs",
          T("کوچک‌ترین تغییر وزن که اجرا می‌شود", "Smallest weight change that is traded"),
          T("به‌جز فروش کامل.", "Except a full sale."), unit=P, lo=0.1, hi=50),
    Field("config", "cash_buffer_frac", "pct", "costs", T("ذخیره‌ی تومانیِ خرج‌نشده", "IRT cash kept unspent"),
          unit=P, lo=0, hi=10),
    # ------------------------------------------------------------------ news
    Field("kimi", "news.enabled", "bool", "news", T("خبر روشن باشد", "News on"),
          T("خاموش: به کیمی گفته می‌شود خبری در دست نیست.", "Off: Kimi is told that no news is available.")),
    Field("kimi", "news.mode", "choice", "news", T("روش تهیه‌ی خبر", "How the news is gathered"),
          T("فید: ربات تیترهای تازه‌ی منابع معتبر را از فید خودشان می‌خواند و مدل فقط خبرهای مهم را انتخاب و خلاصه "
            "می‌کند؛ لینک و تاریخ هر خبر از خود فید است. جست‌وجو: مدل خودش در وب می‌گردد (روش قبلی که از تونل جواب "
            "نداد).",
            "Feeds: the bot reads the latest headlines of the trusted sources from their own feeds and the model only "
            "picks and summarises the important ones; each item's link and date come from the feed. Search: the model "
            "searches the web itself (the old way, which did not work through the tunnel)."),
          choices=[("feeds", T("فید منابع معتبر (پیشنهادی)", "The trusted sources' feeds (recommended)")),
                   ("search", T("جست‌وجوی وب به دست مدل", "The model's own web search"))]),
    Field("kimi", "news.max_calls_per_day", "int", "news",
          T("سقف تهیه‌ی خبر در روز", "News briefs per day (cap)"), lo=0, hi=48),
    Field("kimi", "news.after_hold_only", "bool", "news",
          T("بعد از تصمیم «نگه‌دار» خلاصه‌ی خبر قبلی دوباره استفاده شود",
            "Reuse the previous news summary after a HOLD decision"),
          T("تا ۴۸ ساعت؛ بیدارباش‌ها همیشه خبر تازه می‌گیرند.", "For up to 48 hours; wake-ups always get fresh news.")),
    Field("kimi", "news.cache_minutes", "int", "news", T("عمر خلاصه‌ی خبر", "Lifetime of a news summary"),
          unit=MINUTES, lo=0, hi=10080),
    Field("kimi", "news.max_stale_minutes", "int", "news",
          T("استفاده از خلاصه‌ی کهنه وقتی جست‌وجو شکست خورد", "Use an older summary when the search fails"),
          T("۰ یعنی هرگز.", "0 = never."), unit=MINUTES, lo=0, hi=10080),
    Field("kimi", "news.max_items", "int", "news", T("تعداد خبر در خلاصه", "News items in a summary"), lo=1, hi=20),
    Field("kimi", "news.extra_topics", "longtext", "news",
          T("موضوع‌های اضافه برای خبر", "Extra topics for the news"),
          T("به انگلیسی، حداکثر ۱۰۰۰ نویسه. اینجا دستور معامله ننویسید.",
            "In English, up to 1000 characters. No trading instructions here."), max_len=1000),
    Field("kimi", "news.sources", "domains", "news", T("منابع معتبر خبر", "Trusted news sources"),
          T("فقط خبرِ این سایت‌ها به کیمی می‌رسد (زیردامنه‌ها هم حساب‌اند) و خبر سایت‌های دیگر کنار گذاشته می‌شود. "
            "در روش فید، فید داخلی همین سایت‌ها خوانده می‌شود. هر سایت در یک خط، مثل reuters.com.",
            "Kimi gets news from these sites only (subdomains count); items from other sites are left out. In feeds "
            "mode the built-in feeds of these sites are read. One site per line, e.g. reuters.com."),
          min_items=1, max_items=100),
    Field("kimi", "news.feeds", "urls", "news", T("فیدهای اضافه", "Extra feeds"),
          T("نشانی فید RSS یا Atom سایت‌های دیگر، هر کدام در یک خط (حداکثر ۳۰). خبرشان فقط وقتی می‌آید که آن سایت در "
            "«منابع معتبر خبر» هم باشد. خالی یعنی فقط فیدهای داخلی.",
            "RSS or Atom feed URLs of other sites, one per line (at most 30). Their headlines count only when the site "
            "is also in Trusted news sources. Empty = the built-in feeds only."), nullable=True, max_items=30),
    # ------------------------------------------------------------------ budget
    Field("kimi", "llm.max_calls_per_day", "int", "budget",
          T("سقف تماس با مدل تصمیم در روز", "Decision-model calls per day (cap)"), lo=1, hi=500),
    Field("kimi", "llm.max_tokens_per_day", "int", "budget",
          T("سقف توکن مدل تصمیم در روز", "Decision-model tokens per day (cap)"),
          T("خالی = بدون سقف (توصیه نمی‌شود).", "Empty = no cap (not recommended)."),
          lo=1000, hi=10 ** 9, nullable=True),
    Field("kimi", "brain.reserve_llm_calls", "int", "budget",
          T("تماس‌های رزرو برای بازبینی و وتو", "Calls kept for reviews and vetoes"), lo=0, hi=50),
    Field("kimi", "news.max_tokens_per_day", "int", "budget", T("سقف توکن خبر در روز", "News tokens per day (cap)"),
          T("خالی = بدون سقف.", "Empty = no cap."), lo=1000, hi=10 ** 9, nullable=True),
]

BY_KEY = OrderedDict((f.key, f) for f in FIELDS)
BY_PATH = dict(((f.file, f.path), f) for f in FIELDS)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}_(IRT|USDT)$")
_COIN_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DIGITS = dict((ord(a), ord(b)) for a, b in zip("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
_LIST_SPLIT = re.compile(r"[\s,،;؛]+")
TRUE_WORDS = ("on", "1", "true", "yes")


class FieldError(ValueError):
    """A value that is not acceptable; .text is the (Persian, English) message, str() the English one."""

    def __init__(self, fa, en):
        ValueError.__init__(self, en)
        self.text = T(fa, en)


# ------------------------------------------------------------------ documents
def load_docs(config_text, kimi_text):
    """Parse both files keeping the key order (raises ValueError on invalid JSON / a non-object)."""
    docs = []
    for name, text in (("config.json", config_text), ("kimi.json", kimi_text)):
        doc = json.loads(text, object_pairs_hook=OrderedDict)
        if not isinstance(doc, dict):
            raise ValueError("%s must contain a JSON object" % name)
        docs.append(doc)
    return docs[0], docs[1]


def code_defaults():
    """The built-in value of every section (what applies when a key is missing from the files)."""
    from bitpin import analysis, brain, llm, news, risk, runner
    cfg = copy.deepcopy(runner.DEFAULT_CONFIG)
    cfg["risk"] = copy.deepcopy(risk.DEFAULT_RISK)
    kimi = {"llm": copy.deepcopy(llm.DEFAULT_LLM_CONFIG), "brain": copy.deepcopy(brain.DEFAULT_BRAIN_CONFIG),
            "context": copy.deepcopy(analysis.DEFAULT_CONTEXT_CONFIG),
            "news": copy.deepcopy(news.DEFAULT_NEWS_CONFIG), "guard": copy.deepcopy(analysis.DEFAULT_GUARD_CONFIG)}
    return {"config": cfg, "kimi": kimi}


def _walk(doc, path):
    """The value at path, MISSING when a key is missing, None when a section on the way is null."""
    cur = doc
    for k in path:
        if cur is None:
            return None
        if not isinstance(cur, dict) or k not in cur:
            return MISSING
        cur = cur[k]
    return cur


def effective(doc, field, defaults=None):
    """The value in force: the file's, else the built-in default (MISSING when neither is known)."""
    v = _walk(doc, field.path)
    if v is MISSING or (v is None and _section_is_null(doc, field)):
        d = _walk((defaults or {}).get(field.file, {}), field.path)
        return d if d is not MISSING else MISSING
    return v


def _section_is_null(doc, field):
    cur = doc
    for k in field.path[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
        if cur is None:
            return True
    return False


# ------------------------------------------------------------------ display
def _dec(v):
    return Decimal(repr(v)) if isinstance(v, float) else Decimal(v)


def _dec_text(d):
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def num_text(v):
    """A number as the owner reads it: 15.0 -> "15", 0.35 -> "0.35", never "1e-05"."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return json.dumps(v, ensure_ascii=False)
    return _dec_text(_dec(v))


def display(field, value):
    """The input's text for a stored value (bool: "on" or "")."""
    k = field.kind
    if value is MISSING:
        return ""
    if k == "bool":
        return "on" if value is True else ""
    if value is None:
        return "null" if k == "choice" else ""
    try:
        if k == "pct":
            return _dec_text(_dec(value) * 100)
        if k in ("int", "float"):
            return num_text(value)
        if k == "choice":
            return str(value)
        if k in ("text", "longtext"):
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if k in ("domains", "urls") and isinstance(value, list):
            return "\n".join(str(x) for x in value)          # a textarea: one site (one feed) per line
        if k in LIST_KINDS:
            if not isinstance(value, list):
                return json.dumps(value, ensure_ascii=False)
            return ", ".join(num_text(x) if k == "numbers" else str(x) for x in value)
    except (TypeError, ValueError, InvalidOperation):
        pass
    return json.dumps(value, ensure_ascii=False)


def form_values(config_doc, kimi_doc, defaults=None):
    """{field.key: input text} of the settings in force (missing keys show the built-in default)."""
    if defaults is None:
        defaults = code_defaults()
    docs = {"config": config_doc, "kimi": kimi_doc}
    return OrderedDict((f.key, display(f, effective(docs[f.file], f, defaults))) for f in FIELDS)


# ------------------------------------------------------------------ parsing the posted form
def _clean_number(text):
    s = str(text).translate(_DIGITS).strip()
    s = s.replace("٫", ".").replace("−", "-").replace("٬", "").replace(",", "").replace("_", "").replace(" ", "")
    return s


def _parse_decimal(text):
    s = _clean_number(text)
    if not re.match(r"^[+-]?(\d+(\.\d*)?|\.\d+)$", s):
        raise FieldError("عدد معتبر نیست", "not a valid number")
    return Decimal(s)


def _check_bounds(field, d):
    if field.lo is not None and d < _dec(field.lo):
        raise FieldError("باید حداقل %s باشد" % num_text(field.lo), "must be at least %s" % num_text(field.lo))
    if field.hi is not None and d > _dec(field.hi):
        raise FieldError("باید حداکثر %s باشد" % num_text(field.hi), "must be at most %s" % num_text(field.hi))


def _split_list(text):
    s = str(text).translate(_DIGITS).replace("−", "-").strip()
    return [x for x in _LIST_SPLIT.split(s) if x]


def _count(field, items):
    if field.min_items is not None and len(items) < field.min_items:
        raise FieldError("حداقل %d مورد لازم است" % field.min_items, "at least %d entries are needed" % field.min_items)
    if field.max_items is not None and len(items) > field.max_items:
        raise FieldError("حداکثر %d مورد مجاز است" % field.max_items, "at most %d entries are allowed" % field.max_items)
    return items


def _dedupe(items):
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def parse_value(field, text):
    """The stored value of one posted input (raises FieldError with a Persian and an English message)."""
    k = field.kind
    if k == "bool":
        return text is not None and str(text).strip().lower() in TRUE_WORDS
    if text is None:
        raise FieldError("ارسال نشد", "not sent")
    raw = str(text)
    s = raw.strip()
    if k == "choice":
        for value, _label in field.choices:
            if s == ("null" if value is None else value):
                return value
        raise FieldError("گزینه‌ی نامعتبر", "not one of the choices")
    if k in ("text", "longtext"):
        s = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        if field.max_len is not None and len(s) > field.max_len:
            raise FieldError("حداکثر %d نویسه (الان %d)" % (field.max_len, len(s)),
                             "at most %d characters (now %d)" % (field.max_len, len(s)))
        return s
    if s == "" and (field.nullable or k == "coins_or_null"):
        return None
    if k in ("int", "float", "pct"):
        d = _parse_decimal(s)
        _check_bounds(field, d)
        if k == "int":
            if d != d.to_integral_value():
                raise FieldError("باید عدد صحیح باشد", "must be a whole number")
            return int(d)
        if k == "pct":
            return float(d / 100)
        return float(d)
    if k == "urls":                                       # v3.7: feed URLs, one per line (commas are URL text)
        from bitpin.news import NewsConfigError, check_feed_urls
        out = []
        for x in [u for u in re.split(r"\s+", s) if u]:
            try:
                out.extend(check_feed_urls([x]))
            except NewsConfigError:
                raise FieldError(u"%s نشانی فید معتبر نیست (مثل https://www.example.com/rss)" % x[:60],
                                 "%s is not a feed URL (like https://www.example.com/rss)" % x[:60])
        return _count(field, _dedupe(out))
    items = _split_list(s)
    if k == "numbers":
        out = []
        for x in items:
            d = _parse_decimal(x)
            _check_bounds(field, d)
            out.append(int(d) if d == d.to_integral_value() else float(d))
        return _count(field, _dedupe(out))
    if k == "times":
        out = []
        for x in items:
            m = _TIME_RE.match(x)
            if not m:
                raise FieldError("ساعت %s معتبر نیست (مثل 19:00)" % x[:10], "%s is not a time (like 19:00)" % x[:10])
            out.append("%02d:%s" % (int(m.group(1)), m.group(2)))
        return _count(field, _dedupe(out))
    if k == "domains":
        from bitpin.news import normalize_source
        out = []
        for x in items:
            d = normalize_source(x)
            if d is None:
                raise FieldError("%s دامنه‌ی یک سایت نیست (مثل reuters.com)" % x[:40],
                                 "%s is not a site domain (like reuters.com)" % x[:40])
            out.append(d)
        return _count(field, _dedupe(out))
    rx = _SYMBOL_RE if k == "symbols" else _COIN_RE
    out = []
    for x in items:
        x = x.upper()
        if not rx.match(x):
            raise FieldError("%s معتبر نیست" % x[:24], "%s is not valid" % x[:24])
        out.append(x)
    return _count(field, _dedupe(out))


def parse_form(form):
    """(changes, errors) from the posted form {field.key: text}: one change per field (the helper keeps
    only those that differ from the settings in force); errors {field.key: Text(Persian, English)}. A bool
    field that is absent is False (an unchecked checkbox is not posted)."""
    changes, errors = [], OrderedDict()
    for f in FIELDS:
        text = form.get(f.key)
        if isinstance(text, list):
            text = text[-1] if text else None
        try:
            value = parse_value(f, text)
        except ValueError as e:
            errors[f.key] = e.text if isinstance(e, FieldError) else T(str(e), str(e))
            continue
        changes.append({"file": f.file, "path": list(f.path), "value": value})
    return changes, errors


# ------------------------------------------------------------------ applying (the helper side)
def same(a, b):
    """Equal as settings: 24 == 24.0, [-20] == [-20.0], but True is not 1 and "1" is not 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return _dec(a) == _dec(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, str) and isinstance(b, str):
        return a.replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()
    return type(a) is type(b) and a == b


def check_value(field, value):
    """Check a value that arrived as JSON (the helper does not trust the web side); raises ValueError."""
    k = field.kind
    if k == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if k == "choice":
        if not any(value == v and type(value) is type(v) for v, _l in field.choices):
            raise ValueError("not one of the choices")
        return value
    if value is None:
        if field.nullable or k == "coins_or_null":
            return None
        raise ValueError("may not be empty")
    if k in ("text", "longtext"):
        if not isinstance(value, str):
            raise ValueError("must be a text")
        if field.max_len is not None and len(value) > field.max_len:
            raise ValueError("at most %d characters" % field.max_len)
        return value
    if k in ("int", "float", "pct"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        if k == "int" and not isinstance(value, int):
            raise ValueError("must be a whole number")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        _check_bounds(field, _dec(value) * 100 if k == "pct" else _dec(value))
        return value
    if not isinstance(value, list):
        raise ValueError("must be a list")
    if k == "numbers":
        for x in value:
            if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x:
                raise ValueError("must be a list of numbers")
            _check_bounds(field, _dec(x))
    elif k == "domains":
        from bitpin.news import normalize_source
        for x in value:
            if not isinstance(x, str) or normalize_source(x) != x:
                raise ValueError("invalid entry %r" % (str(x)[:40],))
    elif k == "urls":
        from bitpin.news import NewsConfigError, check_feed_urls
        for x in value:
            try:
                ok = isinstance(x, str) and check_feed_urls([x]) == [x]
            except NewsConfigError:
                ok = False
            if not ok:
                raise ValueError("invalid feed URL %r" % (str(x)[:60],))
    else:
        rx = _TIME_RE if k == "times" else (_SYMBOL_RE if k == "symbols" else _COIN_RE)
        for x in value:
            if not isinstance(x, str) or not rx.match(x):
                raise ValueError("invalid entry %r" % (str(x)[:24],))
    if len(set(json.dumps(x) for x in value)) != len(value):
        raise ValueError("duplicate entries")
    return _count(field, value)


def apply_changes(config_doc, kimi_doc, changes, defaults=None):
    """(new_config_doc, new_kimi_doc, applied, errors). Every change must name a FIELDS key and pass
    check_value; only values that differ from the settings in force are written (a no-op save changes
    nothing, so it never forces a new live confirmation). A section that is null in the file ("news":
    null = switched off) is not re-created here: that is refused with an error."""
    if defaults is None:
        defaults = code_defaults()
    docs = {"config": copy.deepcopy(config_doc), "kimi": copy.deepcopy(kimi_doc)}
    applied, errors = [], []
    if not isinstance(changes, list):
        return docs["config"], docs["kimi"], applied, ["changes must be a list"]
    seen = set()
    for ch in changes:
        if not isinstance(ch, dict) or not isinstance(ch.get("path"), list) or ch.get("file") not in docs:
            errors.append("malformed change %s" % json.dumps(ch, ensure_ascii=False)[:120])
            continue
        path = tuple(str(p) for p in ch["path"])
        field = BY_PATH.get((ch["file"], path))
        if field is None:
            errors.append("%s:%s is not a key of the trade settings form" % (ch["file"], ".".join(path)[:80]))
            continue
        if field.key in seen:
            errors.append("%s is changed twice" % field.key)
            continue
        seen.add(field.key)
        try:
            value = check_value(field, ch.get("value"))
        except ValueError as e:
            errors.append("%s: %s" % (field.key, e))
            continue
        doc = docs[field.file]
        cur = effective(doc, field, defaults)
        if cur is not MISSING and same(cur, value):
            continue
        if _section_is_null(doc, field):
            errors.append("%s: the section %s is null (switched off) in %s.json - turn it on in the JSON editor "
                          "first" % (field.key, ".".join(field.path[:-1]), field.file))
            continue
        box = doc
        bad = False
        for k in field.path[:-1]:
            if k not in box:
                box[k] = OrderedDict()
            box = box[k]
            if not isinstance(box, dict):
                errors.append("%s: %s is not an object in %s.json" % (field.key, k, field.file))
                bad = True
                break
        if bad:
            continue
        box[field.path[-1]] = value
        applied.append({"file": field.file, "path": field.dotted, "old": None if cur is MISSING else cur,
                        "new": value})
    return docs["config"], docs["kimi"], applied, errors


# ------------------------------------------------------------------ English help from the examples
_EXAMPLES = {}


def help_en(field, root_dir):
    """The English "_<key>" comment of the key in <root_dir>/config.example.json or kimi.example.json."""
    name = "config.example.json" if field.file == "config" else "kimi.example.json"
    path = os.path.join(root_dir, name)
    if path not in _EXAMPLES:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                _EXAMPLES[path] = json.load(fh)
        except (OSError, ValueError):
            _EXAMPLES[path] = None
    doc = _EXAMPLES[path]
    box = _walk(doc, field.path[:-1]) if doc is not None else MISSING
    if not isinstance(box, dict):
        return None
    text = box.get("_" + field.path[-1])
    return text if isinstance(text, str) else None
