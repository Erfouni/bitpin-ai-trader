# -*- coding: utf-8 -*-
"""The two languages of the management panel (v3.2): English and Persian.

The panel's texts are written in English (bitpin/panel_web.py); FA below maps each of them to its Persian
translation. The language of a request is picked once, in PanelApp.handle: the cookie __Host-bplang that the
language switch sets, else the browser's Accept-Language (negotiate), else English. It is kept in a thread-local
for the rest of that request only - scripts/panel_server.py handles every request in its own thread and
PanelApp.handle calls reset_lang() when the request ends.

    tr("Dashboard")                  "داشبورد" while a Persian request is handled, else "Dashboard"
    tr(Text(u"روز", "days"))         a pair written next to its data (bitpin/panel_settings.py)
    N_("Running")                    marks a text that is translated later, when it is shown

tests/test_panel_i18n.py checks that every literal given to tr() or N_() in bitpin/panel_web.py has a Persian
translation here, and that nothing here is left unused.
"""
import re
import threading
from collections import namedtuple

LANGS = ("en", "fa")
DEFAULT_LANG = "en"
RTL_LANGS = ("fa",)
LANG_NAMES = (("en", "English"), ("fa", u"فارسی"))

Text = namedtuple("Text", "fa en")
Text.__doc__ = "A (Persian, English) pair; tr() picks the one of the current language."

_state = threading.local()
_TAG_RE = re.compile(r"^\s*([A-Za-z]{1,8})(?:-[A-Za-z0-9]{1,8})*\s*(?:;\s*[qQ]\s*=\s*([0-9.]{1,5}))?\s*$")


def current():
    """The language of the request being handled in this thread."""
    lang = getattr(_state, "lang", None)
    return lang if lang in LANGS else DEFAULT_LANG


def set_lang(lang):
    _state.lang = lang if lang in LANGS else DEFAULT_LANG


def reset_lang():
    _state.lang = None


class use(object):
    """with use("fa"): ... renders in Persian (tests, one-off renders); the previous language comes back."""

    def __init__(self, lang):
        self.lang = lang
        self._prev = None

    def __enter__(self):
        self._prev = getattr(_state, "lang", None)
        set_lang(self.lang)
        return self

    def __exit__(self, *exc):
        _state.lang = self._prev
        return False


def is_rtl(lang=None):
    return (lang or current()) in RTL_LANGS


def N_(msg):
    """Marks an English text for translation where it is shown (tables of labels)."""
    return msg


def tr(msg):
    """msg in the current language: a Text pair, or an English text looked up in FA (unknown: as written)."""
    if isinstance(msg, Text):
        return msg.fa if current() == "fa" else msg.en
    if current() == "fa" and isinstance(msg, str):
        return FA.get(msg, msg)
    return msg


def negotiate(accept_language):
    """The best of LANGS for an Accept-Language header ("fa-IR,fa;q=0.9,en;q=0.8" -> "fa"); DEFAULT_LANG when
    none of them is asked for. Equal weights keep the header's order."""
    best, best_q = None, 0.0
    for part in str(accept_language or "")[:1024].split(",")[:40]:
        m = _TAG_RE.match(part)
        if not m:
            continue
        try:
            q = float(m.group(2)) if m.group(2) is not None else 1.0
        except ValueError:
            continue
        lang = m.group(1).lower()
        if lang in LANGS and q > best_q:
            best, best_q = lang, q
    return best or DEFAULT_LANG


FA = {
    # ---------------------------------------------------------------- layout, common words
    "Bitpin AI Trader": u"معامله‌گر بیت‌پین",
    "Control panel": u"پنل مدیریت",
    "Main menu": u"منوی اصلی",
    "Language": u"زبان",
    "Log out": u"خروج",
    "Overview": u"نمای کلی",
    "Trading": u"معامله",
    "System": u"سیستم",
    "Dashboard": u"داشبورد",
    "Trade settings": u"تنظیمات معامله",
    "Models & keys": u"مدل‌ها و کلیدها",
    "Apply settings": u"اعمال تنظیمات",
    "VPN": "VPN",
    "Logs": u"لاگ‌ها",
    "JSON editor": u"ویرایش JSON",
    "Security": u"امنیت",
    "Running": u"در حال اجرا",
    "Starting": u"در حال روشن شدن",
    "Stopping": u"در حال توقف",
    "Reloading": u"در حال بارگذاری دوباره",
    "Stopped": u"متوقف",
    "Error": u"خطا",
    "Not installed": u"نصب نشده",
    "Nothing to show.": u"موردی نیست.",
    "%s (current value)": u"%s (مقدار فعلی)",
    "Yes": u"بله",
    "No": u"خیر",
    "Tehran": u"تهران",
    "OK": u"موفق",
    "Failed": u"ناموفق",
    "Detect from the address": u"تشخیص از روی آدرس",
    "Other (OpenAI-compatible)": u"دیگر (سازگار با OpenAI)",
    "Go to Apply settings": u"رفتن به اعمال تنظیمات",
    "The panel helper returned an error": u"خطا از سرویس کمکی پنل",
    "The request was not carried out. If this repeats, check the helper service on the server "
    "(bitpin-bot-panel-helper).":
        u"درخواست انجام نشد. اگر تکرار شد، سرویس کمکی پنل (bitpin-bot-panel-helper) را روی سرور بررسی کنید.",
    "Details: %s": u"جزئیات: %s",
    "Internal error": u"خطای داخلی",
    "Internal panel error. The details are in the panel service log (journalctl -u bitpin-bot-panel).":
        u"خطای داخلی پنل. جزئیات در لاگ سرویس پنل است (journalctl -u bitpin-bot-panel).",
    "Page not found": u"صفحه پیدا نشد",
    "This address does not exist in the panel.": u"این نشانی در پنل وجود ندارد.",
    "Invalid request.": u"درخواست نامعتبر.",

    # ---------------------------------------------------------------- sign in
    "Sign in": u"ورود",
    "Sign in to manage the trading bot.": u"برای مدیریت ربات معامله‌گر وارد شوید.",
    "Username": u"نام کاربری",
    "Password": u"رمز عبور",
    "6-digit code from your authenticator app": u"کد شش‌رقمی برنامهٔ احراز هویت",
    "Signing in is locked for now after too many failed attempts. Try again in about %s minutes.":
        u"ورود به‌خاطر تلاش‌های ناموفق زیاد موقتاً قفل شده است. حدود %s دقیقهٔ دیگر دوباره امتحان کنید.",
    "The sign-in form had expired. Please sign in again.": u"فرم ورود منقضی شده بود. دوباره وارد شوید.",
    "Wrong username, password or code.": u"نام کاربری، رمز عبور یا کد نادرست است.",
    "Locked for now after too many failed attempts; try again later.":
        u"به‌خاطر تلاش‌های ناموفق زیاد موقتاً قفل است؛ بعداً دوباره امتحان کنید.",
    "The code is wrong.": u"کد نادرست است.",
    "The current password or the code is wrong.": u"رمز فعلی یا کد نادرست است.",

    # ---------------------------------------------------------------- dashboard
    "Live state of the bot, the account and Kimi's last decision": u"وضعیت لحظه‌ای ربات، حساب و آخرین تصمیم کیمی",
    "Saved settings are not applied yet.": u"تنظیمات ذخیره‌شده هنوز اعمال نشده‌اند.",
    "Live trading is confirmed for the settings in force.": u"اجرای واقعی (live) با تنظیمات فعلی تأیید شده است.",
    "The bot's state could not be read completely: %s": u"وضعیت ربات کامل خوانده نشد: %s",
    "<strong>Trading is halted:</strong> the account fell to the drawdown limit.":
        u"<strong>معامله متوقف است:</strong> افت حساب به حد توقف رسیده است.",
    "Status, as /status shows it in Telegram": u"وضعیت، همان که ‎/status در تلگرام نشان می‌دهد",
    "Last decision, as /last shows it in Telegram": u"آخرین تصمیم، همان که ‎/last در تلگرام نشان می‌دهد",
    "Account value": u"ارزش حساب",
    "IRT": u"تومان",
    "Updated %s": u"به‌روزرسانی: %s",
    "Profit / loss": u"سود / زیان",
    "Started with %s IRT": u"سرمایهٔ شروع: %s تومان",
    "Trading halts at %s": u"توقف معامله در افت %s",
    "peak %s": u"سقف %s",
    "Drawdown": u"افت از سقف",
    "Model cost today": u"هزینهٔ مدل امروز",
    "this month %s &middot; in total %s": u"این ماه %s &middot; از ابتدا %s",
    "Resting limit orders": u"سفارش‌های لیمیت باز",
    "on Bitpin's order book": u"در دفتر سفارش بیت‌پین",
    "Kimi's last decision": u"آخرین تصمیم کیمی",
    "No decision recorded yet.": u"هنوز تصمیمی ثبت نشده.",
    "Result": u"نتیجه",
    "Mode": u"حالت",
    "Confidence": u"اطمینان",
    "Valid": u"معتبر",
    "Fallback": u"حالت پشتیبان",
    "Model": u"مدل",
    "Hold": u"نگه‌داشتن (HOLD)",
    "Trade (new weights)": u"معامله (تغییر وزن‌ها)",
    "Error%s: %s": u"خطا%s: %s",
    "Target weights": u"وزن‌های هدف",
    "No target above zero.": u"هیچ وزن هدفی بالای صفر نیست.",
    "Kimi's report": u"گزارش کیمی",
    "Positions": u"موقعیت‌ها",
    "Market": u"بازار",
    "Kind": u"نوع",
    "Amount": u"مقدار",
    "Entry (USDT)": u"قیمت ورود (USDT)",
    "Stop (USDT)": u"حد ضرر (USDT)",
    "Target (USDT)": u"هدف (USDT)",
    "Hold until": u"نگهداری تا",
    "Allocation": u"تخصیص",
    "Ladder": u"نردبان",
    "Services": u"سرویس‌ها",
    "Trading bot": u"ربات معامله",
    "Telegram notifier": u"اعلان‌گر تلگرام",
    "VPN tunnel": u"تونل VPN",
    "Panel": u"پنل",
    "at boot: %s": u"هنگام بوت: %s",
    "since %s": u"از %s",
    "Bot version %s": u"نسخهٔ ربات: %s",
    "Actions": u"کارها",
    "Restart the bot": u"ری‌استارت ربات",
    "Stop the bot": u"توقف ربات",
    "Start the bot": u"روشن کردن ربات",
    "Restart the notifier": u"ری‌استارت اعلان‌گر",
    "Health check": u"بررسی سلامت",
    "Unknown service or action.": u"سرویس یا عمل نامعتبر است.",
    "%s %s: %s. State now: %s": u"%s %s: %s. وضعیت فعلی: %s",
    "done": u"انجام شد",
    "failed": u"ناموفق",
    "The output of bitpin-bot health": u"خروجی دستور bitpin-bot health",
    "Healthy.": u"سالم است.",
    "A problem was reported.": u"مشکلی گزارش شده است.",
    "Report": u"گزارش",

    # ---------------------------------------------------------------- results of a change
    "Problems (nothing is saved)": u"مشکلات (چیزی ذخیره نمی‌شود)",
    "Warnings": u"هشدارها",
    "Checked: no problem found. Nothing is saved yet.": u"بررسی شد: مشکلی پیدا نشد. هنوز چیزی ذخیره نشده است.",
    "Saved.": u"ذخیره شد.",
    "Backup: %s": u"نسخهٔ پشتیبان: %s",
    "Nothing had changed, so nothing was saved.": u"چیزی تغییر نکرده بود؛ چیزی ذخیره نشد.",
    "Not saved.": u"ذخیره نشد.",
    "Differences": u"تفاوت‌ها",
    "No difference from the file on the server.": u"بدون تفاوت با فایل روی سرور.",
    "The settings are saved but not applied yet.": u"تنظیمات ذخیره شد اما هنوز اعمال نشده است.",
    "Until the new settings are confirmed in Apply settings the bot keeps trading with the <strong>previous</strong> "
    "ones, and starting or restarting the bot fails.":
        u"تا وقتی تنظیمات تازه در «اعمال تنظیمات» تأیید نشده‌اند، ربات با تنظیمات <strong>قبلی</strong> معامله "
        u"می‌کند و روشن یا ری‌استارت کردن ربات شکست می‌خورد.",
    "Key": u"کلید",
    "Old value": u"مقدار قبلی",
    "New value": u"مقدار تازه",

    # ---------------------------------------------------------------- models and keys
    "The model that decides, the one that reads the news, and the API keys":
        u"مدلی که تصمیم می‌گیرد، مدلی که خبر می‌خواند و کلیدهای API",
    "kimi.json cannot be read (invalid JSON). Fix it in the JSON editor.":
        u"فایل kimi.json خوانا نیست (JSON نامعتبر). آن را در «ویرایش JSON» درست کنید.",
    "kimi.json is not a JSON object.": u"فایل kimi.json یک شیء JSON نیست.",
    "Models in use": u"مدل‌های فعلی",
    "Decisions (llm)": u"تصمیم‌گیری (llm)",
    "News (news)": u"اخبار (news)",
    "not in kimi.json, or switched off": u"در kimi.json نیست یا خاموش است",
    "Stage": u"مرحله",
    "Provider": u"ارائه‌دهنده",
    "Address": u"آدرس",
    "Reasoning": u"استدلال",
    "API keys": u"کلیدهای API",
    "Name": u"نام",
    "State": u"وضعیت",
    "The key of each provider": u"کلید هر ارائه‌دهنده",
    "The key follows the provider and is sent only to that provider.":
        u"کلید از روی ارائه‌دهنده تعیین می‌شود و فقط به سکوی همان ارائه‌دهنده فرستاده می‌شود.",
    "Add or change a key": u"ثبت یا تغییر کلید",
    "Key name": u"نام کلید",
    "Service address (LLM_API_KEY only)": u"آدرس سرویس (فقط برای LLM_API_KEY)",
    "LLM_API_KEY is bound to this host and is never sent anywhere else.":
        u"کلید LLM_API_KEY به همین میزبان بسته می‌شود و به هیچ جای دیگری فرستاده نمی‌شود.",
    "Remove this key": u"حذف این کلید",
    "The value is write-only: the panel never shows it. After saving, restart the bot.":
        u"مقدار فقط نوشته می‌شود و پنل هرگز آن را نشان نمی‌دهد. پس از ثبت، ربات را ری‌استارت کنید.",
    "Save the key": u"ثبت کلید",
    "set, %s characters": u"ثبت شده، %s نویسه",
    "not set": u"ثبت نشده",
    "Browse a provider's models": u"فهرست مدل‌های ارائه‌دهنده",
    "API address (base_url)": u"آدرس API (base_url)",
    "Empty = the provider's default address (Moonshot: %s, OpenRouter: %s). The key follows the provider.":
        u"خالی = آدرس پیش‌فرض ارائه‌دهنده (Moonshot: %s، OpenRouter: %s). کلید از روی ارائه‌دهنده انتخاب می‌شود.",
    "Filter by model name (optional)": u"فیلتر نام مدل (اختیاری)",
    "Load the model list": u"بارگیری فهرست مدل‌ها",
    "Choose a model": u"انتخاب مدل",
    "Not sent (the API's default)": u"ارسال نشود (پیش‌فرض API)",
    "Empty = the provider's default address. Keys: Moonshot uses %s, OpenRouter %s, other %s.":
        u"خالی = آدرس پیش‌فرض ارائه‌دهنده. کلید Moonshot: %s، کلید OpenRouter: %s، کلید دیگر: %s.",
    "Model id": u"شناسهٔ مدل",
    "Reasoning effort (decision stage only)": u"عمق فکر (فقط مرحلهٔ تصمیم)",
    "Prices: US dollars per million tokens": u"قیمت‌ها: دلار برای هر میلیون توکن",
    "Input": u"ورودی",
    "Output": u"خروجی",
    "Cached input": u"ورودی کش‌شده",
    "All empty = the prices stay as they are. An empty cached price = the input price.":
        u"همه خالی = قیمت‌ها تغییر نمی‌کنند. ورودی کش‌شدهٔ خالی = همان قیمت ورودی.",
    "Check (no save)": u"بررسی (بدون ذخیره)",
    "Unknown provider.": u"ارائه‌دهندهٔ نامعتبر.",
    "The API address must start with https:// (enter the address for \"Other\" and \"Detect from the address\").":
        u"آدرس API باید با https:// شروع شود (برای «دیگر» و «تشخیص از روی آدرس» آدرس را وارد کنید).",
    "Provider: %s &#8212; key: %s": u"ارائه‌دهنده: %s &#8212; کلید: %s",
    "Models (%s)": u"مدل‌ها (%s)",
    "Id": u"شناسه",
    "Context": u"پنجرهٔ متن",
    "Input $/M": u"ورودی $/M",
    "Output $/M": u"خروجی $/M",
    "Cached $/M": u"کش $/M",
    "Choose": u"انتخاب",
    "Unknown stage.": u"مرحلهٔ نامعتبر.",
    "The API address must start with https://.": u"آدرس API باید با https:// شروع شود.",
    "The model id is empty or not valid.": u"شناسهٔ مدل خالی یا نامعتبر است.",
    "Unknown reasoning effort.": u"عمق فکر نامعتبر.",
    "Model saved": u"مدل ذخیره شد",
    "Unknown key name.": u"نام کلید نامعتبر.",
    "The value is too long.": u"مقدار بیش از حد بلند است.",
    "Either enter a new value or tick Remove, not both.": u"یا مقدار تازه وارد کنید یا «حذف» را بزنید، نه هر دو.",
    "The value is empty. To remove the key, tick Remove.": u"مقدار خالی است. برای حذف کلید، گزینهٔ «حذف» را بزنید.",
    "A key must not contain spaces or line breaks.": u"مقدار کلید نباید فاصله یا خط تازه داشته باشد.",
    "For LLM_API_KEY enter the https address of the service the key belongs to.":
        u"برای LLM_API_KEY آدرس https سرویسی را که کلید مال آن است وارد کنید.",
    "Key %s saved: %s characters.": u"کلید %s ثبت شد: %s نویسه.",
    "Key %s removed.": u"کلید %s حذف شد.",
    "Restart the bot for the change to take effect.": u"برای اثر گرفتن، ربات باید ری‌استارت شود.",

    # ---------------------------------------------------------------- JSON editor
    "config.json and kimi.json as they are on the server": u"فایل‌های config.json و kimi.json همان‌طور که روی سرور هستند",
    "Edit config.json and kimi.json directly. Check only validates; Save validates, writes and keeps a backup of the "
    "previous file. Most trade settings are easier to change in <a href=\"/trade\">Trade settings</a>.":
        u"ویرایش مستقیم config.json و kimi.json. «بررسی» فقط اعتبارسنجی می‌کند؛ «ذخیره» پس از اعتبارسنجی می‌نویسد "
        u"و از فایل قبلی نسخهٔ پشتیبان می‌گیرد. بیشتر تنظیمات معامله در "
        u"<a href=\"/trade\">تنظیمات معامله</a> ساده‌تر عوض می‌شوند.",
    "Invalid request (unknown file, empty text or more than 1 MB).":
        u"درخواست نامعتبر (فایل ناشناخته، متن خالی یا بیش از ۱ مگابایت).",
    "Check": u"بررسی",
    "Save": u"ذخیره",

    # ---------------------------------------------------------------- trade settings
    "Kimi's style, the risk guards, the schedule, the markets and the model budget":
        u"سبک کیمی، محافظ‌های ریسک، زمان‌بندی، بازارها و بودجهٔ مدل",
    "The trade settings form is not available: %s": u"فرم تنظیمات معامله در دسترس نیست: %s",
    "The settings files cannot be read: %s. Fix them in the <a href=\"/settings\">JSON editor</a>.":
        u"فایل‌های تنظیمات خوانا نیستند: %s. آن‌ها را در <a href=\"/settings\">ویرایش JSON</a> درست کنید.",
    "Some values are not valid; each error is shown next to its field. Nothing was saved.":
        u"بعضی از مقدارها نامعتبرند؛ خطا کنار هر مورد نوشته شده. چیزی ذخیره نشد.",
    "Preview only shows what would change. After Save, the new settings must be confirmed in Apply settings before "
    "the bot uses them.":
        u"«پیش‌نمایش» فقط نشان می‌دهد چه چیزی عوض می‌شود. پس از «ذخیره»، تنظیمات تازه باید در «اعمال تنظیمات» "
        u"تأیید شوند تا ربات از آن‌ها استفاده کند.",
    "Sections": u"بخش‌ها",
    "%s to fix": u"%s مورد نیاز به اصلاح",
    "Preview shows the changes without saving anything.": u"پیش‌نمایش، تغییرها را بدون ذخیرهٔ چیزی نشان می‌دهد.",
    "Preview changes": u"پیش‌نمایش تغییرات",
    "More detail": u"توضیح بیشتر",
    "More detail (in English)": u"توضیح کامل (انگلیسی)",
    "Changes (preview; nothing saved yet)": u"تغییرها (پیش‌نمایش؛ هنوز ذخیره نشده)",
    "Saved changes": u"تغییرهای ذخیره‌شده",
    "No value changes.": u"هیچ مقداری تغییر نمی‌کند.",
    "Apply now": u"اعمال همین حالا",

    # ---------------------------------------------------------------- apply
    "Confirm the saved settings for live trading": u"تأیید تنظیمات ذخیره‌شده برای معاملهٔ واقعی",
    "This cannot be confirmed right now; see the text below.": u"در حال حاضر قابل تأیید نیست؛ متن زیر را ببینید.",
    "To confirm, type exactly: %s": u"برای تأیید، دقیقاً این عبارت را تایپ کنید: %s",
    "Start the bot after the confirmation": u"ربات بعد از تأیید روشن شود",
    "Confirm and apply": u"تأیید و اعمال",
    "Change the settings in Trade settings, Models & keys or the JSON editor.":
        u"تنظیمات را در «تنظیمات معامله»، «مدل‌ها و کلیدها» یا «ویرایش JSON» تغییر دهید و ذخیره کنید.",
    "Optionally run the server check below.": u"در صورت تمایل، «بررسی سرور» را در پایین اجرا کنید.",
    "Confirm": u"تأیید",
    "Type the phrase: the bot stops, the settings are confirmed and the bot starts again.":
        u"عبارت تأیید را تایپ کنید: ربات متوقف، تنظیمات تأیید و ربات دوباره روشن می‌شود.",
    "What you are confirming": u"آنچه تأیید می‌کنید",
    "Server check": u"بررسی سرور",
    "Check the server": u"بررسی سرور",
    "Runs bitpin-bot check (up to about 3 minutes) before you confirm; nothing changes.":
        u"اجرای «bitpin-bot check» (تا حدود ۳ دقیقه) پیش از تأیید؛ چیزی تغییر نمی‌کند.",
    "The bot is stopped, the saved settings are confirmed (confirm-live) and the bot starts again.":
        u"ربات متوقف، تنظیمات ذخیره‌شده تأیید (confirm-live) و ربات دوباره روشن می‌شود.",
    "The server check passed.": u"بررسی سرور موفق بود.",
    "The server check found a problem.": u"بررسی سرور مشکل پیدا کرد.",
    "The confirmation phrase is not right. Type exactly %s.": u"عبارت تأیید درست نیست. باید دقیقاً %s تایپ شود.",
    "The settings are confirmed and applied.": u"تنظیمات تأیید و اعمال شد.",
    "Applying the settings did not finish; see the steps below.": u"اعمال تنظیمات کامل نشد؛ مراحل زیر را ببینید.",
    "Bot state: %s": u"وضعیت ربات: %s",
    "Steps": u"مراحل",
    "Step": u"مرحله",
    "Health": u"سلامت",

    # ---------------------------------------------------------------- VPN
    "The xray tunnel the bot reaches Kimi and Telegram through": u"تونل xray که ربات از طریق آن به کیمی و تلگرام وصل می‌شود",
    "No summary available.": u"خلاصه‌ای در دسترس نیست.",
    "Server (the proxy outbound)": u"سرور (خروجی proxy)",
    "No proxy outbound was found in the configuration.": u"خروجی proxy در تنظیمات پیدا نشد.",
    "Inbounds": u"ورودی‌ها (inbounds)",
    "Outbounds": u"خروجی‌ها (outbounds)",
    "Protocol": u"پروتکل",
    "Port": u"پورت",
    "Local only": u"فقط محلی",
    "A proxy inbound listens on every interface and can be reached from outside the server.":
        u"یک پروکسی ورودی روی همهٔ رابط‌ها باز است و از بیرون سرور هم در دسترس است.",
    "Local proxies: %s": u"پروکسی‌های محلی: %s",
    "Restart the tunnel": u"ری‌استارت تونل",
    "Start": u"روشن کردن",
    "Stop": u"خاموش کردن",
    "Test the connection through the proxy": u"آزمایش اتصال از طریق پروکسی",
    "Show the raw configuration": u"نمایش تنظیمات خام",
    "Current connection (secrets hidden)": u"اتصال فعلی (اطلاعات محرمانه پوشانده شده)",
    "Tunnel state": u"وضعیت تونل",
    "Configuration file": u"فایل تنظیمات",
    "The current configuration could not be read: %s": u"تنظیمات فعلی خوانده نشد: %s",
    "New connection from a link": u"اتصال تازه از لینک",
    "Share link (%s)": u"لینک اشتراک (%s)",
    "Preview": u"پیش‌نمایش",
    "Preview first, then apply. If the test after applying fails, the previous configuration is restored "
    "automatically.":
        u"اول پیش‌نمایش، بعد اعمال. اگر آزمایش پس از اعمال شکست بخورد، تنظیمات قبلی خودکار برگردانده می‌شود.",
    "Raw xray configuration": u"تنظیمات خام xray",
    "<strong>Warning:</strong> this text contains the VPN connection's secrets (ids and passwords). Do not copy it "
    "anywhere or show it to anyone.":
        u"<strong>هشدار:</strong> این متن شامل اطلاعات محرمانهٔ اتصال VPN است (شناسه‌ها و رمزها). آن را جایی کپی "
        u"نکنید و به کسی نشان ندهید.",
    "Keep it even if the test fails": u"حتی اگر آزمایش ناموفق بود نگه دار",
    "Save and apply": u"ذخیره و اعمال",
    "Connection test": u"آزمایش اتصال",
    "Proxy": u"پروکسی",
    "Hosts the bot needs": u"مقصدهای لازم برای ربات",
    "Target": u"مقصد",
    "Time (ms)": u"زمان (ms)",
    "The raw text is not available: %s": u"متن خام در دسترس نیست: %s",
    "xray configuration test: passed": u"آزمایش پیکربندی با xray: موفق",
    "xray configuration test: failed": u"آزمایش پیکربندی با xray: ناموفق",
    "Written.": u"نوشته شد.",
    "Not written.": u"نوشته نشد.",
    "The test after applying failed and the previous configuration was restored.":
        u"آزمایش پس از اعمال شکست خورد و تنظیمات قبلی برگردانده شد.",
    "Tunnel state: %s": u"وضعیت تونل: %s",
    "Test through the proxy": u"آزمایش از طریق پروکسی",
    "Test after the restore": u"آزمایش پس از برگرداندن",
    "Summary of the new configuration": u"خلاصهٔ تنظیمات تازه",
    "Differences (secrets hidden)": u"تفاوت‌ها (اطلاعات محرمانه پوشانده شده)",
    "The link is not valid: one link starting with %s, without spaces.":
        u"لینک نامعتبر است: فقط یک لینک که با %s شروع شود، بدون فاصله.",
    "Preview (not applied yet)": u"پیش‌نمایش (هنوز اعمال نشده)",
    "Apply this connection": u"اعمال همین اتصال",
    "No preview is waiting (or it expired). Enter the link again.":
        u"پیش‌نمایشی در انتظار نیست (یا منقضی شده). لینک را دوباره وارد کنید.",
    "The configuration text is empty or too long.": u"متن تنظیمات خالی یا بیش از حد بلند است.",
    "Check result": u"نتیجهٔ بررسی",

    # ---------------------------------------------------------------- logs
    "The latest lines of the services' logs (journalctl)": u"آخرین خط‌های لاگ سرویس‌ها (journalctl)",
    "Service": u"سرویس",
    "Lines (up to 500)": u"تعداد خط (حداکثر ۵۰۰)",
    "Show the log": u"نمایش لاگ",
    "Unknown service.": u"سرویس نامعتبر.",

    # ---------------------------------------------------------------- security
    "Password, two-step login and the audit log": u"رمز عبور، ورود دومرحله‌ای و گزارش رویدادها",
    "Change the password": u"تغییر رمز عبور",
    "Current password": u"رمز فعلی",
    "New password": u"رمز تازه",
    "New password again": u"تکرار رمز تازه",
    "Current code from your authenticator app": u"کد فعلی برنامهٔ احراز هویت",
    "At least 12 characters and three of: lower case, upper case, digits, symbols; not the username or a common "
    "password. Every other session is signed out after the change.":
        u"دست‌کم ۱۲ نویسه و سه نوع از: حرف کوچک، حرف بزرگ، رقم، نماد؛ بدون نام کاربری و رمزهای رایج. پس از تغییر، "
        u"همهٔ نشست‌های دیگر بسته می‌شوند.",
    "Two-step login (TOTP)": u"ورود دومرحله‌ای (TOTP)",
    "On.": u"روشن است.",
    "Current code": u"کد فعلی",
    "Turn off two-step login": u"خاموش کردن ورود دومرحله‌ای",
    "Turn on two-step login": u"روشن کردن ورود دومرحله‌ای",
    "Add this key to your authenticator app (Google Authenticator, Aegis, ...) as a time-based key with 6 digits "
    "and 30 seconds:":
        u"این کلید را در برنامهٔ احراز هویت (Google Authenticator، Aegis، ...) وارد کنید؛ نوع: مبتنی بر زمان، "
        u"۶ رقم، ۳۰ ثانیه:",
    "or this address:": u"یا این نشانی را:",
    "This key is shown only on this page, until you confirm it.": u"این کلید فقط تا تأیید، در همین صفحه دیده می‌شود.",
    "The code the app shows": u"کدی که برنامه نشان می‌دهد",
    "Confirm and turn on": u"تأیید و روشن کردن",
    "Cancel": u"انصراف",
    "Off. For a panel reachable from the internet, turning it on is recommended.":
        u"خاموش است. برای پنلی که از اینترنت در دسترس است، روشن کردن آن توصیه می‌شود.",
    "Create a new key": u"ساختن کلید تازه",
    "Recent events (50)": u"رویدادهای اخیر (۵۰ مورد)",
    "Time": u"زمان",
    "Event": u"رویداد",
    "User": u"کاربر",
    "Details": u"جزئیات",
    "Signed in": u"ورود موفق",
    "Failed sign-in": u"ورود ناموفق",
    "Sign-in locked": u"قفل ورود",
    "Signed out": u"خروج",
    "Action": u"عملیات",
    "Request without a valid token": u"درخواست بدون توکن معتبر",
    "The new password and its repetition differ.": u"رمز تازه و تکرار آن یکی نیستند.",
    "The new password must differ from the current one.": u"رمز تازه باید با رمز فعلی فرق داشته باشد.",
    "The new password was not accepted": u"رمز تازه پذیرفته نشد",
    "The password is changed. Every other session was signed out.": u"رمز عبور عوض شد. همهٔ نشست‌های دیگر بسته شدند.",
    "Two-step login is on already; turn it off first to make a new key.":
        u"ورود دومرحله‌ای همین حالا روشن است؛ برای کلید تازه اول آن را خاموش کنید.",
    "No new key is waiting to be confirmed.": u"کلید تازه‌ای در انتظار تأیید نیست.",
    "The code is not right (check the phone's clock), or signing in is locked for now.":
        u"کد درست نیست (ساعت گوشی را بررسی کنید) یا ورود موقتاً قفل است.",
    "Two-step login is on: from now on every sign-in also asks for the app's code. Every other session was signed "
    "out.":
        u"ورود دومرحله‌ای روشن شد: از این پس هر ورود کد برنامه را هم می‌خواهد. همهٔ نشست‌های دیگر بسته شدند.",
    "Two-step login is off.": u"ورود دومرحله‌ای خاموش است.",
    "Two-step login is turned off.": u"ورود دومرحله‌ای خاموش شد.",

    # ---------------------------------------------------------------- prices
    "Both the input and the output price are needed (or leave every price empty).":
        u"قیمت ورودی و خروجی هر دو لازم‌اند (یا همهٔ قیمت‌ها خالی بمانند).",
    "The price %s is not a number.": u"قیمت %s عدد نیست.",
    "The price %s must be between 0 and 1000 dollars per million tokens.":
        u"قیمت %s باید بین ۰ و ۱۰۰۰ دلار برای هر میلیون توکن باشد.",
}
