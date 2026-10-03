# CHANGELOG

نسخه‌ها از بالا به پایین، جدیدترین اول. برای هر نسخه اول خلاصهٔ فارسی (چه چیزی برای مالک عوض می‌شود)، بعد جزئیات انگلیسی به تفکیک بخش‌های مشخصات (A..G) با نام فایل‌ها، کلیدهای config و مطالعهٔ مبدأ هر تغییر. شمارهٔ نسخه در `bitpin/__init__.py` (`__version__`) است و در بنر شروع ربات چاپ می‌شود.

---

## 3.12.0 — خوانش تکنیکال با کد، و تأیید دوم پیش از هر خرید (2026-10-03)

مالک پرسید چطور تحلیل تکنیکال کیمی بهتر شود. بررسی تصمیم‌های واقعی نشان داد کیمی نمودار را درست می‌خواند (خوانشش با کد یکی
بود)؛ ضعفش تبدیل خوانش به احتمال است. چهار پیشنهاد بررسی شد و دوتایش ساخته شد:

- **خوانش تکنیکال را کد می‌دهد.**
  - هر کوینی که دادهٔ کامل دارد، در دادهٔ تصمیم یک خوانش آماده دارد: روند، روند بلند، مومنتوم، RSI، بولینگر، دانچیان و حجم،
    با همان قاعده‌های ثابت نسخهٔ ۳٫۸.
  - کیمی دیگر این‌ها را دوباره حساب نمی‌کند؛ فقط جمع‌بندی خودش (صعودی، نزولی یا خنثی) را می‌نویسد و فکرش را صرف خبر، ریسک و
    استدلال مخالف می‌کند.
  - صفحهٔ تحلیل تکنیکال پنل برای این خوانش‌ها برچسب «با کد» دارد.
- **خرید فقط با تأیید دوم.**
  - روزی که تصمیم روزانه یا بیدارباش حرکت قیمت چیزی می‌خرد، کیمی یک بار دیگر با همان داده پرسیده می‌شود. هر کوین فقط تا جایی
    خریده می‌شود که هر دو جواب بخرند (مقدار کمترِ دو جواب)؛ بقیه در تتر می‌ماند.
  - اگر جواب دوم نرسد، رد شود یا وقت تصمیم تمام شده باشد، آن روز چیزی خریده نمی‌شود. فروش‌ها و باقی تصمیم سر جایشان می‌مانند.
  - فقط روزهای خرید یک درخواست اضافه دارند.
  - کارت «آخرین تصمیم کیمی» در داشبورد ردیف «پرسش دوم» دارد: تأیید شد، یا خرید کم شد و چقدر.
  - تنظیمش در «تنظیمات معامله ← سبک» است: «خرید فقط با تأیید دوم کیمی».
- **ساخته نشد (مطالعهٔ ۰۸):**
  - **احتمال از داده:** احتمالی که از همین عددهای تکنیکال روی دورهٔ آموزش ساخته شد، در دورهٔ آزمون بدتر از احتمال بی‌مزیت
    بود و حتی برعکس عمل کرد.
  - **بازهٔ هفتگی و روزانه** (روند ۱۲ هفته، میانگین ۵۰ و ۲۰۰ روزه): بدترش کرد.
  - مالک خواسته بود این دو فقط اگر در دورهٔ آزمون بهتر بودند ساخته شوند.
- **یافتهٔ مهم:** در دورهٔ آزمون، روزهای ستاپ (همان شرط‌هایی که کیمی با آن‌ها می‌خرد) نصف روزهای معمولی به هدف رسیدند: ۱۴٪ در
  برابر ۲۸٪. کیمی برای همین روزها ۰٫۱۵ به احتمال اضافه می‌کرد. این عدد حالا در دانشی هست که کیمی هر روز می‌خواند. محدود کردن
  این بالابر در کد پیشنهاد شده و منتظر تصمیم مالک است.
- English:
  - `bitpin/technical.py`: `context_text`, `code_ta`, `method_text` (the code's reading), `schema_text` (`"read"`), and
    the v3.8 texts kept as `*_v38`.
  - `bitpin/analysis.py`: `_ta_readings` ("ta" on every full-detail coin, `LEGEND_TA`).
  - `bitpin/brain.py`:
    - `parse_analysis`: the candidate's ta = `code_ta` + read, `ta_source`; an old-style "ta" is still checked;
    - `_confirm_buys` and `Decision.confirmation`;
    - config `brain.confirm_buys`;
    - the validate arguments shared in `_decide`;
    - the CONFIRMATION mechanics line.
  - `bitpin/notify.py`: `_confirmation`.
  - `scripts/panel_helper.py`, `bitpin/panel_web.py`, `bitpin/panel_settings.py`, `bitpin/panel_i18n.py`.
  - `docs/STRATEGY_KNOWLEDGE.md`: study 08, and study 07 shortened.
  - Study 08: in the development repository.
  - Tests: `tests/test_confirm_and_reading.py` and the updated pins. Test brains run with `confirm_buys` off: one fake reply
    per decision.

## 3.11.0 — نگه‌داشتن موقعیت تا شکست سطح ابطال، و مکث پیش از خرید دوباره (2026-10-03)

ربات یک روز همهٔ کوین‌ها را فروخت و روز بعد BTC را گران‌تر دوباره خرید. مالک خواست جلوی این رفت‌وبرگشت گرفته شود. اول
تحقیقی دربارهٔ قانون‌های ربات‌های معامله‌گر هوش مصنوعی دیگر انجام شد، بعد پیشنهادها روی دادهٔ خود بیت‌پین آزمایش شدند
(مطالعهٔ ۰۷؛ هر دو در مخزن توسعه).

- **موقعیتِ برنامه‌دار فروخته نمی‌شود، مگر با محرک مشخص.** در تصمیم روزانه و بیدارباش حرکت قیمت، کد فروش (یا کم کردن) کوینی را که
  برنامهٔ کیمی را دارد اجرا نمی‌کند و وزنش را همان که هست نگه می‌دارد. فروش فقط با یکی از این‌ها آزاد می‌شود:
  - بسته‌شدن ساعتی زیر سطح ابطال برنامه (کیمی بیدار می‌شود و می‌تواند بفروشد یا برنامه را دوباره بنویسد)؛
  - رسیدن قیمت به هدف برنامه؛
  - خبری از خلاصهٔ خبر همان تصمیم که نام همان کوین را دارد (هک، حذف از بازار، مشکل صرافی)؛ کیمی باید تیتر را عیناً در
    `exit_news` بیاورد و کد آن را با تیترهای خلاصه مقایسه می‌کند. خبر کلی بازار کافی نیست.

  حالت‌های کاهش ریسک (افت حساب)، وتو، بررسی پر شدن نردبان و تصمیم پایانی مثل قبل می‌توانند بفروشند.
- **پایان مهلت برنامه دلیل فروش نیست.** برنامه با همان سطح ابطال سر جایش می‌ماند. اگر کیمی برنامه را دوباره بنویسد، سطح ابطال
  قبلی می‌ماند؛ سطح تازه فقط با خرید بیشتر می‌آید. حد ضرری که بالاتر از سطح ابطال باشد هم پذیرفته نمی‌شود.
- **مکث ۷۲ ساعته پیش از خرید دوباره.** بعد از فروش با حد ضرر یا با تصمیم، همان کوین تا ۷۲ ساعت خریده نمی‌شود، مگر قیمتش دست‌کم
  ۳٪ زیر قیمت فروش باشد. فروش در هدف شامل نیست. نردبان سقوط مثل قبل کار می‌کند.
- **چرا (مطالعهٔ ۰۷):** روی ۹۰۰ روز دادهٔ ساعتی بیت‌پین با همان ستاپ‌های ربات، در ۱۱۱ پنجرهٔ ۹۰ روزه:
  - آزمودن دوبارهٔ هر روزهٔ کوینِ در دست (همان کاری که ربات کرد): ۲۳ رفت‌وبرگشت در سال برای هر کوین؛ میانگین هر پنجره +۷٫۶٪ در
    دورهٔ آموزش و −۲٫۴٪ در دورهٔ آزمون؛
  - آزمودن دوباره در پایان مهلت برنامه: ۱۵ رفت‌وبرگشت؛ +۸٫۵٪ و +۰٫۱٪؛
  - نگه‌داشتن تا شکست سطح ابطال: ۸ رفت‌وبرگشت؛ +۱۲٫۱٪ و +۶٫۵٪ (خرید و نگه‌داری +۱۱٫۶٪ و +۹٫۶٪).

  بالا بردن هفتگی سطح ابطال همین برتری را از بین برد، برای همین سطح ثابت می‌ماند. حاشیهٔ ۱ یا ۲٪ روی سود مورد انتظار نتیجهٔ
  دوگانه داشت (آموزش بهتر، آزمون بدتر) و ساخته نشد.
- **پنل:**
  - تنظیمات معامله ← سبک: سه تنظیم تازه («موقعیتِ برنامه‌دار تا شکست سطح ابطال نگه داشته شود»، «مکث خرید دوباره پس از فروش» و
    «خرید دوباره در مکث، اگر این‌قدر ارزان‌تر شد»).
  - هر کارت موقعیت می‌گوید با چه چیزی فروخته می‌شود.
  - کارت تازهٔ «فروخته‌شده‌های اخیر» کوین‌های در مکث را با زمان و قیمتِ آزاد شدن نشان می‌دهد.
- این‌ها پیش‌فرض کد هستند و تأیید دوباره لازم ندارند. برای برگرداندن رفتار قبلی، تنظیم اول را خاموش و مکث را صفر کنید.
- English:
  - `bitpin/brain.py`:
    - `protected_positions`, `parse_exit_news`, `names_coin`, `COIN_NAMES` and `HOLD_MODES`;
    - `validate_response(protected=, news_headlines=, hold_discipline=)`: a protected coin is kept at its current weight,
      with the money taken back from USDT_IRT, then IRT cash, then the other coins' increases, then their weights; a
      stop above the invalidation is dropped; a restated expired plan keeps its invalidation;
    - `_blocked_increases`: the re-entry cooldown;
    - new config keys `brain.hold_discipline` / `reentry_cooldown_hours` / `reentry_waive_pct`;
    - the prompt's `{{HELD_CANDIDATES}}` / `{{HELD_RETEST}}` / `{{CONSISTENCY_TEXT}}` / `{{EXIT_NEWS_*}}` and the HOLD
      DISCIPLINE mechanics line;
    - each decision record carries its "rules".
  - `bitpin/runner.py`: `_note_sale` records a decision's sale (reason "sold") in `recent_exits`, which are now kept 168 h.
  - `bitpin/analysis.py`: `LEGEND_EXITS`.
  - `bitpin/performance.py`: `read_rules`, `sales_pauses`, `hold_rules`, and the report's "pauses" and each position's
    "hold" / "pause".
  - `bitpin/panel_web.py` / `panel_i18n.py` / `panel_settings.py`.
  - `docs/STRATEGY_KNOWLEDGE.md`: study 07.
  - Study 07 and the research notes: in the development repository.
  - Tests: `tests/test_hold_discipline.py`, plus updated text pins.

## 3.10.1 — درصدهای عدد صحیح درست نمایش داده می‌شوند (2026-10-03)

- پنل درصدی را که بی‌اعشار نشان داده می‌شد اشتباه می‌نوشت: صفرهای آخر عدد را هم حذف می‌کرد. اطمینان ۵۰٪ کیمی در داشبورد «۵٪»
  دیده می‌شد و سهم ۱۰۰٪ تتر در صفحهٔ تحلیل تکنیکال «۱٪». حالا صفرها فقط بعد از ممیز حذف می‌شوند.
- English: `bitpin/panel_web.py` pct_text (zeros are cut only after a decimal point); `tests/test_panel_ta_chart.py`.

## 3.10.0 — موقعیت‌های واقعی جدا از کوین‌هایی که نداریم، و همهٔ تحلیل تکنیکال به دلار (2026-10-03)

- **صفحهٔ عملکرد** حالا دو بخش دارد:
  - **«موقعیت‌های باز»:** فقط کوین‌هایی که حساب واقعاً دارد، یعنی کوینی که ربات برایش برنامه دارد یا ارزشش دست‌کم ۱ تتر است.
    اگر هیچ کوینی نداریم، همین را می‌گوید.
  - **«کوین‌هایی که نداریم: سفارش‌های منتظر»:** کوین‌هایی که ربات فقط سفارش خرید نردبانی رویشان دارد. نمودار، اندیکاتورها و
    آخرین تحلیل کیمی را دارند، با برچسب «در سبد نیست» و حاشیهٔ خط‌چین. ارزش و مهلت نگهداری ندارند. باقی‌ماندهٔ کمتر از حداقل
    سفارشِ یک فروش همین‌طور نام برده می‌شود.
- کارت «موقعیت‌ها»ی داشبورد وقتی موقعیتی نیست، به‌جای جدول خالی همین را می‌نویسد.
- **همهٔ تحلیل تکنیکال به دلار (تتر):**
  - ATR حالا روی کندل‌های ۴ ساعتهٔ تتری حساب می‌شود، نه تومانی، پس افت ریال در آن نیست. حداقل فاصلهٔ ابطال کیمی (۲ برابر
    ATR) هم تتری شد.
  - ارزش معاملات ۲۴ ساعته به **هزار تتر** است (`vol24h_k` به‌جای `vol24h_m` میلیون تومانی).
  - نسبت حجم به میانگین ۳۰ روزه هم تتری است؛ پیش‌تر افت ریال حجم‌های اخیر را بزرگ‌تر نشان می‌داد.
  - نمودار حجم کارت‌ها هم به هزار تتر است.
- **تتر خوانش تکنیکال ندارد:** USDT_IRT نرخ تومان است، نه نمودار یک کوین. کیمی برایش `ta` نمی‌دهد و صفحهٔ تحلیل تکنیکال آن را جدا
  نشان می‌دهد: «تومان در برابر تتر: هر تتر … تومان»، با تغییر ۲۴ ساعت، ۷ روز و ۳۰ روز و سهم تتر از حساب. همان صفحه می‌گوید حساب
  در آن تصمیم کدام کوین‌ها را داشت.
- این‌ها فقط عددهای ورودی کیمی را دقیق‌تر و تتری می‌کنند. منطق معامله و تنظیمات عوض نشده است.
- English: `bitpin/analysis.py` symbol_features (atr4h_pct on the coin's USDT 4h candles - IRT OHLC over the hour's
  USDT_IRT close -, vol24h_k = 24h traded value in thousand USDT replaces vol24h_m, vol_ratio on USDT values; LEGEND);
  `bitpin/technical.py` (reading() gives nothing for USDT_IRT, method_text: USDT_IRT gets "ta": null, collect(): no
  USDT_IRT coin row + "usdt" / "usdt_weight", chart_data traded value in thousand USDT); `bitpin/performance.py`
  (DUST_USDT, a position's "held" / "dust"); `bitpin/panel_web.py` (Open positions / Coins we do not hold, the
  Held / Not held badges, the dashboard's empty positions, the technical page's held line and toman line);
  `bitpin/static/panel.css`; tests.

## 3.9.1 — موجودی منفیِ ساختگی بعد از فروش کامل (2026-10-02)

- کارت BTC در صفحهٔ **عملکرد** بعد از فروش کامل مقدار `-0.00000001` و ارزش منفی نشان می‌داد؛ XRP هم همین‌طور. علت: بیت‌پین
  کارمزدی را که به خود کوین می‌گیرد تا دقت همان کوین گرد می‌کند، ولی ربات کارمزد را با همهٔ رقم‌هایش ثبت می‌کند. موجودی‌ای که
  صفحه از روی معامله‌ها می‌سازد برای همین کمی زیر صفر می‌رفت، در حالی که موجودی واقعی صفر بود.
- حالا اگر یک معامله موجودی یک دارایی را کمتر از ۰٫۱٪ همان معامله زیر صفر ببرد، صفر حساب می‌شود. اختلاف بزرگ‌تر (یعنی
  معامله‌ای که ربات ثبت نکرده) مثل قبل منفی می‌ماند و هشدار «با ارزش ثبت‌شده فرق دارد» را هم می‌دهد.
- `panel-cert` حالا پیش از گرفتن گواهی می‌گوید که certbot حدود ۳۰ ثانیه منتظر DNS می‌ماند و چیزی چاپ نمی‌کند، تا کسی آن را
  گیرکرده نپندارد و Ctrl+C نزند.
- English: `bitpin/performance.py` holdings_at (ROUNDING_FRACTION: a fill that leaves its side below zero by at most
  0.1% of the fill's size leaves zero); `tests/test_performance.py`; `scripts/panel_setup.py` (the wait is announced).

## 3.9.0 — اندیکاتورها روی نمودار هر موقعیت (2026-10-02)

- کارت هر موقعیت و هر سفارش باز در صفحهٔ **عملکرد** حالا روی نمودار قیمتش همان اندیکاتورهایی را نشان می‌دهد که ربات برای
  کیمی حساب می‌کند، با همان روش و همان پنجره (کندل‌های ۴ ساعته به تتر از ۱۷۰۰ کندل ساعتی آخر):
  - **EMA ۲۰ / ۵۰ / ۲۰۰**؛
  - **باند بولینگر (۲۰، ۲)** با خط وسطش؛
  - **کانال دانچیان ۲۰**؛
  - **نزدیک‌ترین حمایت و مقاومت** با فاصله و تعداد برخورد.
- زیر نمودار سه نمودار کوچک با همان محور زمان می‌آید:
  - **RSI ۱۴** با مرزهای ۷۰، ۵۵، ۴۵ و ۳۰؛
  - **MACD (۱۲، ۲۶، ۹)** به درصد قیمت، با هیستوگرام؛
  - **ارزش معاملات هر ۴ ساعت** با میانگین ۳۰ روزه.
- یک خط هم خوانش قاعده‌های «تحلیل تکنیکال» را برای همین لحظه می‌گوید. آخرین نقطهٔ هر خط همان عددی است که ربات اگر همین
  حالا تصمیم بگیرد به کیمی می‌دهد؛ یک تست این را با کد خود ربات می‌سنجد.
- دکمهٔ «پنهان کردن اندیکاتورها» نمودارها را ساده می‌کند و انتخاب با تازه‌سازی خودکار صفحه هم می‌ماند.
- ربات و تصمیم‌هایش عوض نشده‌اند. برای این نمودارها فقط کندل‌های عمومی بیشتری خوانده می‌شود: برای بازار هر موقعیت و تتر،
  ۷۱ روز کندل ساعتی، در یک درخواست.
- English: `bitpin/technical.py` chart_data / trim_chart (the context's own basis: the coin's last 1700 closed hourly
  IRT bars over the USDT_IRT close of the same hour, 4h bars aligned to UTC; EMA, Bollinger, Donchian, RSI, MACD in %
  of the bar's close, traded value per 4h; "now" = symbol_features of the same bars, "reading" = reading() of them);
  `bitpin/performance.py` (TA_FETCH_HOURS: the hourly window of the chart markets and USDT_IRT in a range that ends
  now, report(..., ta), a position's "ta" trimmed to its chart span); `bitpin/panel_web.py` (line_chart bars /
  y_range / x_range, ta_overlay, ta_reading_html, ta_panes, the ta=0 switch kept in the links and the live reload);
  `bitpin/static/panel.css`; `tests/test_technical.py` TestChartData (the last values equal symbol_features),
  `tests/test_performance.py`, `tests/test_panel_ta_chart.py`.

## 3.8.2 — گواهی معتبر برای دامنهٔ پنل (2026-10-02)

- پنل حالا می‌تواند برای یک **دامنه** گواهی معتبر Let's Encrypt بگیرد. مرورگر دیگر هشدار نمی‌دهد و مقایسهٔ اثر انگشت لازم
  نیست: `sudo bitpin-bot panel-cert panel.example.com`.
- پورت‌های ۸۰ و ۴۴۳ سرور معمولاً دست وب‌سرور دیگری است، پس مالکیت دامنه با یک رکورد DNS در **Cloudflare** ثابت می‌شود. دستور
  یک بار توکن API ‌ای را می‌پرسد که مالک فقط برای DNS همان دامنه ساخته است (موقع تایپ دیده نمی‌شود). اول آن را از Cloudflare
  می‌پرسد و بعد در `/etc/bitpin-bot-panel/acme/cloudflare.ini` (فقط root) نگه می‌دارد؛ هیچ‌جا چاپ نمی‌شود.
- **تمدید خودکار:** زمان‌بند تازهٔ `bitpin-bot-panel-cert.timer` روزی دو بار نگاه می‌کند و گواهی را از ۳۰ روز پیش از پایانش
  تمدید می‌کند. پنل گواهی تازه را **بدون بستن نشست‌ها** بار می‌کند (`systemctl reload`). تمدید ناموفق در تلگرام با پیام خودش
  گزارش می‌شود، نه با هشدار «سرویس ربات از کار افتاد».
- **جدا از certbot سایت‌های دیگر سرور:** پوشه‌ها، تنظیمات و زمان‌بند خودش را دارد و `/etc/letsencrypt` و `cli.ini` آن را نه
  می‌خواند و نه دست می‌زند. گواهی خودامضای قبلی نگه داشته می‌شود و `panel-cert --off` آن را برمی‌گرداند.
- **پنل:** صفحهٔ **امنیت** کارت تازهٔ «گواهی پنل» دارد: نام‌ها، صادرکننده، تاریخ پایان و روزهای مانده، تمدید خودکار (روشن /
  خاموش و بررسی بعدی)، نتیجهٔ آخرین بررسی تمدید و اثر انگشت. `panel-status` و `health` هم گواهی را نشان می‌دهند.
- English: `bitpin/panel_cert.py` (new: domain / token checks, the Cloudflare token check with the IPv4 answers first -
  the server's IPv6 route to Cloudflare times out after a minute -, certbot arguments in the panel's own directories,
  installing a lineage for the panel with the self-signed pair kept once, cert_info / summary);
  `scripts/panel_setup.py` (`cert DOMAIN [--new-token] | --off`, internal `cert-deploy` / `cert-renew` / `cert-info` /
  `certbot`: certbot's main with IPv4 first and without the server's other cli.ini); `scripts/panel_server.py`
  (SIGHUP: a new TLS context for the next connections); `deploy/bitpin-bot-panel-cert.{service,timer}` (new, never
  enabled by install / update; OnFailure -> Telegram); `deploy/bitpin-bot-panel.service` (ExecReload);
  `deploy/lib.sh` / `uninstall.sh` / `bitpin-bot` (PANEL_UNITS, the ACME directories, `panel-cert`, a health line);
  `scripts/panel_helper.py` (read-only `panel_cert`); `bitpin/panel_web.py` (the certificate card);
  `bitpin/notify.py` (the renewal's own alert); `tests/test_panel_cert.py`.

## 3.8.1 — صفحهٔ تحلیل تکنیکال خواناتر (2026-10-02)

- جدول «خوانش تکنیکال کیمی» حالا در عرض صفحه جا می‌شود و ستون «بررسی» درست بعد از خوانش کلی می‌آید، پس بدون جابه‌جا کردن
  جدول دیده می‌شود.
- برچسب روند بلند حالا «بالای EMA200» / «زیر EMA200» است (پیش‌تر فقط «بالای آن»).
- برچسب کوین‌های تحلیل‌شده «در تحلیل کیمی» است. پیش‌تر دو ترجمه برای یک متن بود و «زمان تحلیل» نشان داده می‌شد؛ یک تست تازه
  جلوی ترجمهٔ تکراری را می‌گیرد.
- English: `bitpin/panel_web.py` (the check column after the overall reading, the reading table wraps, the badge
  "In Kimi's analysis", the long-trend labels "Above / Below EMA200"); `tests/test_panel_i18n.py`
  test_no_text_is_translated_twice.

## 3.8.0 — «خوانش تکنیکال»: تحلیل تکنیکال کیمی با روش ثابت و دادهٔ ساختاریافته (2026-10-02)

- ربات برای هر کوین عددهای دقیق اندیکاتورها را به کیمی می‌داد (فاصله از EMA20/50/200، RSI، MACD، بولینگر، کانال دانچیان،
  حمایت و مقاومت، حجم). ولی کیمی برداشتش را فقط در یک متن آزاد («شواهد») می‌نوشت که در ذخیره حتی نشانه‌هایی مثل `<` و `=` را
  از دست می‌داد، و معلوم نبود هر عدد را چطور خوانده است.
- حالا یک **روش ثابت** در دستور کیمی نوشته شده و کیمی برای هر کوینی که تحلیل می‌کند یک شیء `ta` با فیلدهای ثابت برمی‌گرداند:
  روند (EMA20 و EMA50)، روند بلند (EMA200)، مومنتوم (هیستوگرام MACD)، RSI (اشباع خرید / قوی / خنثی / ضعیف / اشباع فروش)،
  جای قیمت در باندهای بولینگر، جای قیمت در کانال دانچیان، حجم، نزدیک‌ترین حمایت و مقاومت، و خوانش کلی (مثبت / منفی / خنثی).
- **کد همان قاعده‌ها را روی همان عددها اجرا می‌کند** و هر فیلدی را که کیمی متفاوت خوانده نشان می‌دهد. این بررسی فقط برای
  دیدن است و هیچ تصمیمی را رد نمی‌کند: خوانش تکنیکال وضع نمودار را توصیف می‌کند و به‌تنهایی سیگنال خرید یا فروش نیست
  (پژوهش ربات نشان داده بود سیگنال‌های مکانیکی از هزینه‌ها جلو نمی‌زنند، B7 و B8).
- **پنل:** صفحهٔ تازهٔ **«تحلیل تکنیکال»** (`/technical`): خوانش کیمی برای هر کوینِ تحلیل‌شده با علامت فیلدهای متفاوت و
  مقدار درست، جدول عددهای دقیق همهٔ کوین‌هایی که داده‌ی کامل دارند (همان عددهایی که به کیمی رسید) با برچسب خوانش کد، و توضیح
  روش. کادر «استراتژی و تحلیل» هر موقعیت در صفحهٔ عملکرد هم ردیف «خوانش تکنیکال» دارد و کارت آخرین تصمیم در داشبورد به این
  صفحه لینک می‌دهد.
- هزینه: جواب کیمی برای هر کوینِ تحلیل‌شده حدود ۱۰۰ توکن بلندتر می‌شود. رفتار معامله‌ای کد عوض نشده و تأیید تازه لازم نیست.
- English: new `bitpin/technical.py` - the fixed rules (`reading()`: trend from ema_dev_pct[0..1], long from [2],
  momentum from the MACD histogram vs MACD_FLAT 0.02, rsi bands 70/55/45/30, Bollinger place 0.8/0.2, Donchian
  breakout / halves / breakdown, vol_ratio 1.5/0.7, the nearest sup / res or the channel edges with distance and
  strength, a descriptive tone), `parse_ta()`, `check_ta()` (levels within LEVEL_TOLERANCE 0.5%), `method_text()` /
  `schema_text()` rendered into the system prompt ({{TA_METHOD}}, {{TA_SCHEMA}}: a TECHNICAL READING paragraph after
  PROCEDURE step 4, "ta" after "row" in each candidate) and `collect()` (the last decision record with a context ->
  the panel's data). `bitpin/brain.py` parse_analysis keeps ta, ta_code, ta_check (and ta_dropped) on every candidate
  and logs the differences; never a problem, never an adjustment. USER_MESSAGE_TAIL names "ta". Panel: helper command
  `technical` (ta-worker as bitpin), page `/technical` (navigation: Overview), analysis box row,
  `performance.read_analyses` carries ta / ta_check. Tests: `tests/test_technical.py`, `tests/test_panel_technical.py`.

## 3.7.0 — «خبر از فید»: ربات خبر را از فید خود منابع معتبر می‌خواند (2026-10-01)

- جست‌وجوی وب مدل خبر از راه تونل جواب نداد. Moonshot «حالت JSON» را کنار ابزار جست‌وجویش با جواب خالی رد می‌کند. kimi-k2.6
  بعد از یک جست‌وجو یا چند دقیقه متن پشت سر هم می‌نوشت تا تونل قطعش کند، یا جست‌وجوی بعدی‌اش را فقط به‌صورت متن می‌نوشت. نتیجه:
  از ۲۷ سپتامبر هیچ خلاصهٔ خبر تازه‌ای نیامد.
- حالا (`news.mode` = `"feeds"`، پیش‌فرض) خود ربات تیترهای تازهٔ منابع معتبر را از **فید خودشان** (RSS، Atom، نقشهٔ خبری
  گوگل) می‌خواند. ۲۶ فید از ۱۹ سایت: رویترز، بلومبرگ، بی‌بی‌سی، CNBC، فایننشال تایمز، CoinDesk، The Block، Cointelegraph،
  Decrypt، ایران اینترنشنال، رادیو فردا، فدرال رزرو، SEC، BLS، دنیای اقتصاد، اقتصادنیوز، تجارت‌نیوز، ایسنا و ایرنا. سایت‌های
  خارجی از راه تونل و سایت‌های ایرانی مستقیم خوانده می‌شوند؛ آزمون روی سرور: ۲۶ از ۲۶ فید در ۳ ثانیه، ۱۵۰ تیتر.
- مدل خبر فقط **یک درخواست کوتاه** می‌گیرد: فهرست شماره‌دار تیترهای ۷۲ ساعت اخیر، با حالت JSON و بدون ابزار جست‌وجو. مدل
  خبرهای مهم را انتخاب و به انگلیسی خلاصه می‌کند و هر خبر را فقط با **شمارهٔ تیتر** نشان می‌دهد. لینک و تاریخ هر خبر از خود فید
  برداشته می‌شود، نه از مدل؛ خبری که به تیتری از فهرست اشاره نکند کنار گذاشته می‌شود. پس دادهٔ خبر دقیق و ساختاریافته است.
- اگر هیچ فیدی خوانده نشود، تماسی با مدل گرفته نمی‌شود و تصمیم مثل قبل بدون خبر (یا با آخرین خلاصهٔ سالم) گرفته می‌شود.
- **پنل:** در «تنظیمات معامله ← اخبار» دو تنظیم تازه هست: «روش تهیه‌ی خبر» (فید منابع معتبر یا جست‌وجوی وب مدل) و «فیدهای
  اضافه» (نشانی فید سایت‌های دیگر، هر کدام در یک خط). داشبورد کارت تازهٔ **«خلاصه‌ی خبر»** دارد: خبرهای آخرین خلاصه با سایت،
  زمان و لینک خود مقاله، و اینکه چند فید خوانده شد و کدام‌ها نه.
- سایت‌های بی‌فید: apnews.com (خواندن را رد می‌کند)، wsj.com (فیدهایش از ۲۰۲۵ متوقف شده)، blockworks.co (کهنه) و بیت‌پین
  (فیدی ندارد). خبر این سایت‌ها نمی‌آید مگر نشانی فید تازه‌ای در «فیدهای اضافه» بدهید.
- روش قبلی با `news.mode` = `"search"` هنوز در دسترس است. رفتار معامله‌ای عوض نشده. چون پیش‌فرض کد عوض شده و فایل تنظیمات نه،
  تأیید تازه (`confirm-live`) لازم نیست.
- English: new `bitpin/news_feeds.py` - FEED_TABLE (26 feeds of 19 default sources, each with its route: "proxy" =
  through the news proxy, "direct" for the Iranian sites; the other route once after a network error), parallel
  fetching within FEED_DEADLINE (60 s, FEED_TIMEOUT 20 s per feed, FEED_MAX_BYTES 3 MB), `parse_feed()` (RSS 2.0,
  RSS 1.0 / RDF, Atom, Google News sitemaps; bodies with entity declarations or a DTD internal subset are refused;
  an entry's own title / link / date only - Atom <source>, <author>, sitemap <image:image> and Media RSS skipped),
  `select_headlines()` (a link on news.sources, a known date in the last FEED_MAX_AGE_HOURS 72, duplicates by link
  or title once, FEED_PER_SITE 15 per site, then every site in turn up to FEED_MAX_HEADLINES 150),
  `build_feed_messages()` (numbered headlines, JSON-mode request without tools) and `items_from_refs()` (each item's
  source_url / link / time_hint from its cited headline; unknown or repeated refs dropped). `bitpin/news.py`:
  news.mode ("feeds" default | "search") and news.feeds (extra feed URLs, check_feed_urls), `_feeds_call()` /
  `_feed_request()` / `_default_feed_fetch()` (the hardened API transport per route), NewsBrief.mode and .feeds,
  item "link" (the article, for the panel; the prompt still shows the host only), the cache's "feeds" (the last
  attempt), the prompt block's header names the feeds. `scripts/run_bot.py`: kimi-check prints the feeds and the
  failed ones; the paper test hook simulates search mode. Panel: `news.mode` (choice) and `news.feeds` (new field
  kind "urls") in the trade form, the dashboard's news card (`scripts/panel_helper.py` news_view, PanelApp.
  _news_card). `kimi.example.json` / `news.example.json`: mode, feeds. Tests: `tests/test_news_feeds.py`,
  `tests/test_panel_news.py`; the search-mode tests run with mode "search".

## 3.6.4 — خبر: فرصت جست‌وجو پیش از خواستن JSON (2026-09-30)

- آزمون واقعی ۳٫۶٫۳ روی سرور: مدل خبر بعد از اولین جست‌وجو فقط نوشت «باید دقیق‌تر جست‌وجو کنم» و متوقف شد. نسخهٔ ۳٫۶٫۳ همین
  یک جمله را با درخواست تمیز به JSON تبدیل کرد و نتیجه خلاصه‌ای خالی بود («خبر مهمی نبود») که روز آرامی را نشان می‌داد، نه
  تحقیقی ناتمام را.
- حالا جوابی که JSON نیست، تا وقتی دور جست‌وجو باقی است، یک بار با «اگر چیزی کم است همین حالا جست‌وجو کن و بعد فقط شیء JSON
  را بفرست» جواب می‌گیرد و ابزار جست‌وجو هنوز در دسترس است. بعد از آن، مثل قبل، یک بار دیگر بدون ابزار JSON خواسته می‌شود.
- درخواست تمیز JSON فقط برای جوابی است که دست‌کم یک لینک منبع دارد، یعنی واقعاً گزارش خبر است. هر خبر بدون لینک منبع به
  هر حال حذف می‌شود.
- رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `bitpin/news.py`: SEARCH_NOW_NUDGE - a complete reply that is not the JSON object, while search rounds are
  left and the tool is not withdrawn, is answered once with the model's text given back and "call the search tool
  now if something is missing, then reply with only the JSON object", the tool still offered; FORMAT_NUDGE (the tool
  withdrawn) follows as before. `_restructure()` only for a reply with at least one link (`_LINK_RE`), in the format
  retry and in the length retry. Tests: `tests/test_news_format.py`, `tests/test_news_json.py`,
  `tests/test_news_sources.py`, `tests/test_news.py`.

## 3.6.3 — خبر روی Moonshot: جواب خالی یعنی «حالت JSON پذیرفته نشد» (2026-09-30)

- آزمون واقعی ۳٫۶٫۲ روی سرور نشان داد که Moonshot «حالت JSON» را کنار ابزار جست‌وجوی داخلی خودش (`$web_search`) با خطای
  HTTP 400 رد نمی‌کند، بلکه جواب خالی با `finish_reason: unexpected_state` می‌دهد. نسخهٔ ۳٫۶٫۲ این را شکست تحقیق خبر حساب
  می‌کرد. حالا جواب خالی به درخواستی با حالت JSON یعنی «رد شد»: همان درخواست بدون حالت JSON (و با ابزار جست‌وجو) دوباره
  فرستاده می‌شود و این برای بقیهٔ عمر سرویس به خاطر سپرده می‌شود.
- اگر جواب مدل متن آزاد باشد، یا پیش از کامل شدن شیء JSON قطع شود، آن متن در یک درخواست تمیز و جدا به شیء JSON با ساختار
  ثابت تبدیل می‌شود: فقط قواعد تحقیق خبر و همان متن به‌عنوان یادداشت، با حالت JSON، بدون ابزار و بدون تاریخچهٔ جست‌وجو. اگر
  API این را هم نپذیرد، مثل قبل یک بار دیگر در همان گفت‌وگو خواسته می‌شود.
- رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `bitpin/news.py`: an empty reply (any finish_reason but length) to a request with JSON mode counts as a
  refusal like an HTTP 400 - Moonshot answers JSON mode next to its builtin $web_search with an empty reply and
  finish_reason "unexpected_state" (kimi-check, 2026-09-30 13:44 UTC) - so `_json_with_tools` (or `_json_mode`) goes
  off and the same request is sent again. `NewsResearcher._restructure()`: a prose reply (the format retry) or a reply
  cut by max_tokens before its JSON object (the length retry) is turned into the JSON object by a clean request: the
  research system prompt plus RESTRUCTURE_PROMPT with the reply as notes (at most RESTRUCTURE_NOTES_CHARS), JSON mode,
  no tools and no tool history; when that is refused too (HTTP 400 or an empty reply) JSON mode goes off and the old
  nudge inside the conversation follows. Tests: `tests/test_news_format.py`, `tests/test_news_sources.py`.

## 3.6.2 — خبر فقط به‌صورت JSON و نجات جواب‌های قطع‌شده (2026-09-30)

- مدل خبر (kimi-k2.6) گاهی به‌جای شیء JSON متن آزاد و خیلی طولانی می‌نوشت (۲۹ سپتامبر: ۸۰۰۰ توکن). جوابی به این درازی چند
  دقیقه جریان دارد و تونل آن را وسط راه قطع می‌کرد (۳۰ سپتامبر: دو بار پشت سر هم). حالا بعد از اولین جست‌وجو، و در هر درخواست
  بدون ابزار جست‌وجو، «حالت JSON» خود API (`response_format: json_object`) خواسته می‌شود: جواب فقط می‌تواند همان شیء JSON با
  ساختار ثابت باشد، پس کوتاه و خواناست. اگر API این حالت را نپذیرد، همان درخواست بدون آن فرستاده می‌شود و این برای بقیهٔ عمر
  سرویس به خاطر سپرده می‌شود.
- جوابی که تونل وسط راه قطع کند ولی شیء JSON کامل، یا دست‌کم ۳ خبر کاملِ آن، پیش از قطع رسیده باشد، همان استفاده می‌شود و
  درخواست پولی دوباره فرستاده نمی‌شود.
- لاگ هر قطعی مدت درخواست و حجم رسیده را هم می‌گوید تا رفتار تونل سنجیده شود.
- رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `bitpin/news.py`: JSON_MODE (`response_format: {"type": "json_object"}`) on every tool-loop request after the
  first search round and on every request without the tool; an HTTP 400 on such a request turns it off for the
  requests with the tool (`_json_with_tools`) or for all (`_json_mode`) and sends the request again.
  `partial_stream_reply()` reads what a cut stream delivered; `NewsResearcher._salvage_cut()` uses a cut stream whose
  content holds the whole reply object or at least SALVAGE_CUT_MIN_ITEMS (3) complete items (never a cut tool round),
  with its usage estimated from what arrived; network and stream errors in the log carry the seconds and bytes.
  Tests: `tests/test_news_json.py`, `tests/test_news.py`.

## 3.6.1 — پیش‌بینی و تحلیل هر موقعیت در پنل (2026-09-30)

- نمودار هر موقعیت باز حالا **آینده** را هم نشان می‌دهد: ناحیهٔ کمرنگ از «الان» تا پایان مهلت نگهداری برنامه.
  - **دو سناریوی کیمی** از لحظه و قیمتِ آخرین تصمیمش: رسیدن به هدف (خط‌چین سبز) با احتمالی که خود کیمی داده، و خوردن به سطح
    ابطال یا تمام شدن مهلت (خط‌چین قرمز) با احتمال باقی‌مانده. **قیمت مورد انتظار** (نقطه‌چین) میانگین وزن‌دار این دو با همان
    احتمال‌هاست.
  - **محدودهٔ نوسان عادی** (سایهٔ دولایه): جایی که قیمت در یک بازار بی‌جهت، با نوسان واقعی ساعتی اخیرِ همان کوین، در ۶۸٪ و
    ۹۵٪ مواقع می‌ماند (قیمت × e^(±zσ√t)؛ همان فرض «بازار بی‌جهت» که کیمی احتمال پایه‌اش را با آن می‌سنجد).
  - سطح ابطال برنامه (خط‌چین قرمز افقی) هم به خط‌های ورود، حد ضرر، هدف و سفارش‌ها اضافه شد.
- زیر هر موقعیت، جعبهٔ **«استراتژی و تحلیل»** به فارسی:
  - استراتژی (مثلاً «ادامهٔ روند» با شرطش) و ردیف جدول نرخ پایه (مثلاً B10)؛
  - روش‌های تحلیل (تکنیکال، کلان، آماری، اخبار) از روی فیلدهایی که کیمی به‌عنوان شاهد آورده؛
  - احتمال کیمی در برابر احتمال بازار بی‌جهت، سود تا هدف، زیان تا ابطال، کارمزد و ارزش انتظاری؛
  - حکم کیمی، زمان تحلیل، و متن اصلی شواهد و سناریوی مخالف کیمی.
- کیمی این داده‌ها را از قبل در هر تصمیم می‌داد (بخش analysis پاسخ تصمیم، در `kimi_decisions.jsonl`). پس دستور کیمی عوض
  نشده و هزینه‌ای اضافه نمی‌شود؛ هر تصمیم تازه این بخش را به‌روز می‌کند.
- رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `bitpin/performance.py`: `read_analyses()` (the newest analysis candidate of each coin from the valid,
  non-fallback decisions in kimi_decisions.jsonl; Kimi's own texts from its reply when that can be read),
  `hourly_sigma()`, `outlook()` (the 68% / 95% range of a driftless lognormal walk from now to the plan's max hold,
  the take-profit / invalidation scenarios from Kimi's decision point, the probability-weighted price
  p x TP + (1 - p) x INV), `outlook_end()`; a position's price chart reaches back as far as its outlook runs ahead
  (7..30 days). `bitpin/panel_web.py`: `line_chart(areas=, shade=)`, the outlook legend, `analysis_html()` (setup,
  base-rate row, the methods of the cited fields, p against p0, reward / risk / cost, EV, verdict, time, Kimi's
  words). Tests: `tests/test_performance.py` (TestOutlook), panel tests.

## 3.6.0 — عملکرد، نمودارهای زنده و سابقهٔ معاملات در پنل (2026-09-30)

- صفحهٔ تازهٔ **عملکرد** (`/performance`): سود و زیان حساب برای هر بازهٔ زمانی (۲۴ ساعت، ۷ روز، ۳۰ روز، از شروع، یا روزهای
  دلخواه به وقت تهران)، هم **به تومان** و هم **به تتر**، یعنی بدون اثر افت ریال. کنارش نشان می‌دهد اگر همهٔ سرمایه فقط تتر
  نگه داشته می‌شد چقدر می‌شد، و ربات چند واحد درصد از آن جلوتر یا عقب‌تر است. تعداد معامله‌ها و کارمزدها هم هست.
- **سود و زیان هر دارایی جدا** (BTC، ETH، ...، تتر و تومان نقد) به تومان و به تتر: ارزش پایان − ارزش شروع − پولی که وارد شده +
  پولی که بیرون آمده، هر معامله با قیمت خودش. جمع ردیف‌ها دقیقاً تغییر حسابی است که از روی معامله‌های ربات بازسازی می‌شود. اگر
  با ارزش ثبت‌شده بیش از ۲٪ فرق کند (مثلاً معامله یا واریزی که ربات نکرده)، هشدار داده می‌شود.
- **نمودارهای زنده**: درصد تغییر ارزش سبد (به تومان، به تتر، و «فقط تتر»)، و برای هر موقعیت باز نمودار قیمت به تتر با خط ورود،
  حد ضرر، هدف، سفارش‌های خرید و فروشِ باز و زمان ثبت برنامه. نقطهٔ آخر هر نمودار همین دقیقه است. صفحه هر دقیقه خودش تازه
  می‌شود (بدون جاوااسکریپت) و با یک دکمه خاموش می‌شود. تازه‌سازی خودکار نشست را زنده نگه نمی‌دارد، پس خروج خودکار بعد از
  بی‌کاری مثل قبل کار می‌کند.
- صفحهٔ تازهٔ **سابقهٔ معاملات** (`/history`): همهٔ خریدها و فروش‌های ربات، جدید به قدیم، با فیلتر دارایی و نوع، صفحه‌به‌صفحه،
  و **دانلود CSV** برای اکسل.
- حساب‌ها فقط‌خواندنی‌اند: از فایل‌های خود ربات (با کاربر `bitpin`، نه root) و کندل‌های **عمومی** بیت‌پین؛ هیچ کلید، هیچ
  درخواست احرازشده و هیچ سفارشی در کار نیست. برای بازه‌های بلند، قیمت‌های قدیمی‌تر از ۱۰ روز از کندل‌های ۴ ساعته می‌آیند تا
  گزارش در تمام سال سبک بماند.
- رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `bitpin/performance.py` (new): `report()` - totals from the recorded equity (the value at t_from is the last
  record at or before it, else the start value), each value converted to USDT at its own hour's USDT_IRT close,
  rows per asset (value change minus net flows, each fill at its own price, fees against the asset traded; the rows
  add up to the change of the account rebuilt from the fills; >2% rebuilt/recorded mismatch = warning), the
  estimate of the value at this minute (the last record moved by the latest prices), chart series thinned to 360
  points, the open positions (plan, resting limit orders, 7..30 days of prices in USDT) and the history (newest
  5000); `collect()` - reads kimi_runner.jsonl / kimi_equity.json / live_orders.json, clamps a range to one hour
  before the first record, fetches hourly candles for the last 10 days and 4-hour candles before.
  `scripts/panel_helper.py`: command `performance` (from / to validated, at most 400 days) run by `perf-worker` as
  the user bitpin, output limit 3 MB (`run_command(limit=)`). `bitpin/panel_web.py`: `/performance`, `/history`,
  `/history.csv` (CSV-injection-safe cells, BOM), SVG line charts stretched to their box with HTML axis labels
  (`line_chart`, `nice_ticks`), a 45 s report cache per range, `<meta http-equiv="refresh">` with `auto=1` that does
  not touch the session (`SessionStore.get(touch=False)`); Persian texts in `panel_i18n.py`, styles in `panel.css`.
  Tests: `tests/test_performance.py`, panel and helper tests.

## 3.5.3 — جواب خبرِ بی‌قالب و قطعی اتصال تونل (2026-09-30)

- آزمون واقعی بعد از ۳٫۵ نشان داد مدل خبر (kimi-k2.6) با دستور تازهٔ منابع گاهی به‌جای JSON یک متن معمولی می‌نویسد (دو بار
  پشت سر هم). حالا: (۱) دستور منابع نرم‌تر است: فیلتر `site:` فقط کمک است و اگر جست‌وجوی فیلترشده چیزی پیدا نکرد، بدون فیلتر
  جست‌وجو می‌شود و فقط نتیجه‌های سایت‌های فهرست نگه داشته می‌شود؛ (۲) جوابی که JSON نیست یک بار دیگر خواسته می‌شود و متن خود
  مدل به او برگردانده می‌شود تا همان را در قالب JSON بنویسد؛ (۳) لاگ، جست‌وجوهای انجام‌شده و آغاز جواب خوانده‌نشده را نشان
  می‌دهد.
- وقتی خودِ اتصال از تونل برقرار نشود (مثلاً «TLS handshake timed out»)، درخواست هرگز فرستاده نشده؛ این حالت هم مثل «هیچ
  جوابی نیامد» حساب می‌شود و تلاش‌های اضافهٔ ۳٫۵٫۲ را می‌گیرد.
- رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `bitpin/news.py`: FORMAT_NUDGE (a complete reply without the JSON object is asked for once more, its own text
  given back, the tool withdrawn; the OpenRouter plugin request is repeated with FORMAT_NUDGE_PLAIN), the softer
  SOURCES rule (a filtered search that finds nothing is repeated without the filter), the search queries in the
  log, `_excerpt` of an unreadable reply; `_one_request`: a URLError whose reason is a timeout (CONNECT / TLS
  handshake) is raised as `no connection ...` with `no_answer`. Tests: `tests/test_news_format.py`.

## 3.5.2 — تلاش بیشتر برای درخواستی که تونل بلعید (2026-09-30)

- گاهی اولین درخواست بزرگِ بعد از یک دورهٔ بیکاری در تونل (پراکسی محلی) گیر می‌کند و **هیچ** جوابی برنمی‌گردد، ولی درخواست
  بعدی بی‌مشکل رد می‌شود (آزمون روی سرور: یک درخواست ۴۰ کیلوبایتی ۶۰ ثانیه بی‌جواب ماند و سه درخواست بعدی در کمتر از یک ثانیه
  جواب گرفتند؛ درخواست‌های کوچک همیشه رد می‌شدند). تا حالا درخواست تصمیمی که ۱۲۰ ثانیه هیچ جوابی نمی‌گرفت فقط یک بار دوباره
  فرستاده می‌شد و دو شکست پشت سر هم تصمیم را باطل می‌کرد.
- حالا درخواستی که **هیچ** جوابی نگرفته (حتی سرآیند پاسخ) تا ۳ بار دیگر دوباره فرستاده می‌شود، داخل همان مهلت کل تصمیم. جوابی
  که بعد از شروع قطع شود (یعنی Moonshot آن را پردازش و حساب کرده) مثل قبل فقط یک بار دوباره فرستاده می‌شود. هزینهٔ تخمینی هر
  تلاش گم‌شده مثل قبل در بودجه حساب می‌شود.
- رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست. ریشهٔ مشکل در مسیر تونل است (به احتمال زیاد اندازهٔ بسته /
  MTU)؛ این نسخه فقط اثرش را کم می‌کند.
- English: `bitpin/llm.py` SILENT_EXTRA_RETRIES = 3: a streamed POST without any answer (`no_answer`, set by
  `bitpin/news.py` `_one_request` at STREAM_HEADERS_SECONDS and passed on by `make_llm_transport`) is retried up to three
  more times on top of `max_timeout_retries` (the chat's `timeouts` counter is now `[timed out, silent]`); replies lost
  after their headers keep the old limit. Tests: `tests/test_llm_silent.py`.

## 3.5.1 — آزمون واقعی‌تر خبر (2026-09-30)

- `kimi-check --news` به پژوهش خبر ۴۸۰ ثانیه وقت می‌دهد، نه ۱۵۰: یک جست‌وجوی وب و جوابی تا ۱۶۰۰۰ توکن از پشت تونل چند دقیقه
  طول می‌کشد و آزمونِ ۱۵۰ ثانیه‌ای خبرِ سالم را هم «FAILED» نشان می‌داد. خود ربات همچنان `news.deadline_seconds` خودش (۶۰۰
  ثانیه) را دارد. رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `scripts/run_bot.py` NEWS_CHECK_OVERRIDES deadline_seconds 150 -> 480.

## 3.5.0 — «اخبار معتبر و زمان بیشتر برای کیمی» (2026-09-30)

- **اخبار فقط از منابع معتبر** (به درخواست مالک). پژوهشگر خبر فقط در فهرستی از سایت‌های معتبر جست‌وجو می‌کند (با `site:`)
  و هر خبری که از سایت دیگری باشد یا منبع نداشته باشد کنار گذاشته می‌شود، همراه با خلاصه‌ای که مدل نوشته بود (ممکن است بر
  همان خبر تکیه داشته باشد). فهرست پیش‌فرض چهار گروه دارد: خبرگزاری‌های بین‌المللی (Reuters، AP، Bloomberg، BBC، CNBC، FT،
  WSJ)، رسانه‌های کریپتو (CoinDesk، The Block، Cointelegraph، Decrypt، Blockworks)، رسانه‌های اقتصادی و خبری ایران (دنیای
  اقتصاد، اقتصادنیوز، تجارت‌نیوز، ایسنا، ایرنا، ایران اینترنشنال، رادیو فردا) و منابع رسمی (فدرال رزرو، SEC، BLS، بیت‌پین).
  در پنل (تنظیمات معامله ← اخبار ← منابع معتبر خبر) ویرایش می‌شود.
- **جواب نیمه‌کارهٔ خبر.** یک بار مدل خبر به‌جای یک JSON کوتاه ۸۰۰۰ توکن متن نوشت و قطع شد و هیچ خبری به کیمی نرسید. حالا سقف
  جواب ۱۶۰۰۰ توکن است؛ از جوابی که باز هم قطع شود خبرهای کاملش نگه داشته می‌شود؛ اگر هیچ خبر کاملی نداشت یک بار دیگر فقط
  JSON خواسته می‌شود؛ و جوابی که خوانده نشد برای عیب‌یابی در `news_failed_reply.txt` (پوشهٔ state) می‌ماند.
- **زمان بیشتر برای تصمیم کیمی** (به درخواست مالک). تصمیم روزانه با «استدلال حداکثر» می‌تواند بیش از ۷ دقیقه طول بکشد و سقف
  ۴۲۰ ثانیه‌ای هر درخواست آن را قطع می‌کرد. حالا هر درخواست ۹۰۰ ثانیه، هر فراخوان با تلاش دوباره ۱۲۰۰ ثانیه و کل تصمیم ۱۵۰۰
  ثانیه وقت دارد (سقف مجاز هر درخواست ۱۸۰۰). درخواستی که هیچ جوابی نگیرد همچنان بعد از ۱۲۰ ثانیه رها و دوباره فرستاده می‌شود.
- **به‌روزرسانی:** منابع معتبر بلافاصله بعد از `update.sh` کار می‌کنند (پیش‌فرض کد است و تأیید تازه لازم نیست). زمان‌های تازه و
  سقف جواب خبر در `kimi.json` نوشته می‌شوند: `sudo python3 /opt/bitpin-bot/deploy/apply_profile.py`، بعد `confirm-live` و
  راه‌اندازی دوباره (در پنل: «اعمال»).
- English: `bitpin/news.py`: `DEFAULT_NEWS_SOURCES` (25 domains) and `news.sources` (null = that list, [] = any site;
  `normalize_source`, `check_sources`, `source_allowed`); the SOURCES rule of the researcher prompt (site: filters);
  `sanitize_reply(sources=)` leaves out untrusted or unsourced items and then the model's summary
  (`OFF_SOURCE_SUMMARY`, `NOTHING_USABLE_SUMMARY`); `has_recent_news` / `fresh_search_nudge` count only trusted
  items; `salvage_reply` keeps the complete items of a reply cut at max_tokens; `LENGTH_NUDGE` asks once more (the
  tool withdrawn, the cut text not sent back; the OpenRouter plugin gets it appended to the user message);
  `news_failed_reply.txt` (0600, redacted). `news.max_tokens` 16000 (now a profile key of
  `deploy/apply_profile.py`). `bitpin/llm.py`: `llm.timeout` up to 1800. `kimi.example.json`: `llm.timeout` 900,
  `llm.deadline_seconds` 1200, `brain.decision_deadline_seconds` 1500, `news.max_tokens` 16000, `news.sources`.
  Panel: the "Trusted news sources" field (kind `domains`, one site per line; a missing key shows the built-in list and
  saving it unchanged writes nothing). Tests: `tests/test_news_sources.py`; the older news tests run with
  `sources: []`.

## 3.4.1 — مطالعهٔ ۰۶: جای حد ضرر و هدف (2026-09-29)

- به درخواست مالک، روی ۹۰۱ روز دادهٔ ساعتی بیت‌پین آزموده شد که حد ضرر زیر حمایت (یا کف دانچیان) بهتر از درصد ثابت
  است یا نه (پوشهٔ `research/06_stop_placement` در مخزن توسعه): ورود هر روز ساعت ۱۹ تهران، پلان ۱۶۸ ساعته،
  تقسیم آموزش / آزمون مثل همهٔ مطالعه‌ها.
- نتیجه: **جای حد ضرر (حمایت، دانچیان یا ۵٪ ثابت) در میانگین فرق معناداری نمی‌کند**، ولی هر حد ضرری بدترین ضرر یک پلان را
  از حدود ۲۶ تا ۳۱ درصد به ۱۰ تا ۱۹ درصد می‌رساند. حد ضرر خیلی نزدیک (کمینهٔ ۲×ATR) در بیش از نیمی از پلان‌ها زده شد و
  بدترین بود. **هدف سود روی مقاومت بهتر از هدف ثابت نبود** و هدف گذاشتن در ۳ نمونه از ۴ میانگین را پایین آورد.
- برای همین جملهٔ نسخهٔ ۳٫۴ که هدف را به نزدیک‌ترین مقاومت محدود می‌کرد برداشته شد؛ حالا مقاومت فقط «جایی که قیمت ممکن
  است بایستد» است. خلاصهٔ این مطالعه به دانش راهبردی کیمی (`docs/STRATEGY_KNOWLEDGE.md`) اضافه شد. تنظیمات عوض نشده‌اند.
- English: study 06 (`research/06_stop_placement/` in the development repository, `stop_study.py`, `report_*.json`):
  9010 daily plans, 15 stop x target rules, paired. `bitpin/prompt_template.txt`: res no longer caps
  take_profit_usdt; the bare 2 x atr4h_pct minimum is flagged; `docs/STRATEGY_KNOWLEDGE.md`: a "Stops and targets"
  paragraph with the numbers. Tests: the pinned prompt sentences.

## 3.4.0 — «سطح‌های تکنیکال» (2026-09-29)

- **داده‌های تکنیکال بیشتر برای کیمی** (به درخواست مالک)، برای هر کوینی که کامل نشان داده می‌شود (تتر، BTC / ETH / XRP / SOL
  و هر کوین در دست یا زیر نظر)، روی قیمت تتری و کندل‌های ۴ ساعته:
  - **MACD (12, 26, 9)**: خط، سیگنال و هیستوگرام، به درصد قیمت؛
  - **باند بولینگر (20, 2)**: جای قیمت در باند (۰ = باند پایین، ۱ = باند بالا) و پهنای باند؛
  - **کانال دانچیان ۲۰ کندلی**: کف و سقف ۸۰ ساعت اخیر (دستور کیمی از قبل می‌گفت حد ابطال را آنجا بگذارد، ولی خود عدد هیچ‌وقت
    به او داده نمی‌شد)؛
  - **سطح‌های حمایت و مقاومت**: تا ۳ سطح زیر و ۳ سطح بالای قیمت از کف‌ها و سقف‌های ۳۰ روز اخیر (نقطه‌های چرخش ۲۴ ساعته)، با
    تعداد برخورد هر سطح.
- دستور کیمی: حد ابطال را زیر ساختار (کف دانچیان یا یک حمایت) بگذارد، هدفی بالاتر از نزدیک‌ترین مقاومت دلیل شکستن آن را
  می‌خواهد، و MACD و بولینگر فقط شاهد کمکی یک ستاپ‌اند، نه دلیل خرید مستقل (آزمایش‌ها نشان داد سیگنال‌های مکانیکی به‌تنهایی از
  هزینه‌ها جلو نمی‌زنند).
- هر عددی که کیمی از این‌ها نقل کند مثل بقیهٔ داده‌ها با متن بازار مقایسه می‌شود. ورودی کیمی حدود ۹۰۰ نویسه (حدود ۲۵۰ توکن)
  بزرگ‌تر می‌شود. رفتار معامله‌ای و تنظیمات عوض نشده‌اند و تأیید تازه لازم نیست.
- English: `bitpin/indicators.macd`; `bitpin/analysis.py` `macd_pct`, `bollinger_pos_width`, `support_resistance` (4h swing
  points, SR_PIVOT_BARS 6 = 24 h each side, zones of one ATR(14) clamped to 0.3..2.5% of px, SR_LEVELS 3 per side,
  touches counted; an old resistance below the price counts as support) and the fields `macd4h_pct`, `bb4h`,
  `don20_4h`, `sup` / `sup_n`, `res` / `res_n` of the full-detail coins (compact rows unchanged) with their legend;
  `bitpin/prompt_template.txt` step 1 (the two indicators are evidence, never a setup) and step 2 (invalidation at
  `don20_4h[0]` or a support in `sup`; a take profit beyond the nearest `res` needs the case that it breaks). Tests:
  `TestTechnicalLevels` in `tests/test_analysis.py`, the citation check of `sup[i]` in `tests/test_brain.py`, the
  pinned sentences in `tests/test_prompt_template.py`; the context size limits of three tests raised by 2000
  characters.

## 3.3.0 — «اخبار به‌روز» (2026-09-29)

- **خبرهایی که به کیمی می‌رسید کهنه بود.** جست‌وجوی خبر بدون تاریخ انجام می‌شد و مقاله‌های سال قبل را برمی‌گرداند:
  یکی از خلاصه‌ها فقط چهار خبر داشت و هر چهار مال سال قبل بود (خود کیمی هم نوشته بود نتیجه‌ها قدیمی‌اند)، و خلاصهٔ دیگری
  هیچ خبری نداشت. هر بار هم فقط یک جست‌وجو انجام شده بود.
- حالا: تاریخ امروز در دستور جست‌وجو هست و ماه و سال باید در هر جست‌وجو بیاید (مثل «Bitcoin news September 2026»)؛ دست‌کم
  دو جست‌وجو (بازار کریپتو و اقتصاد آمریکا، و اقتصاد ایران و ریال)؛ **خبری که بیش از ۷ روز پیش از جست‌وجو باشد خودکار کنار
  گذاشته می‌شود** و خلاصه می‌گوید چند خبر کهنه کنار رفت؛ اگر جواب هیچ خبر تازه‌ای نداشته باشد یک بار دیگر با تاریخ جست‌وجو
  می‌شود؛ و خلاصهٔ خالی دیگر یک روز کامل نگه داشته نمی‌شود (تصمیم بعدی دوباره جست‌وجو می‌کند).
- **قطعی تونل:** گاهی درخواست‌ها ۵ تا ۷ دقیقه منتظر جوابی می‌ماندند که هرگز نمی‌رسید (هم خبر و هم تصمیم).
  حالا درخواستی که ظرف ۲ دقیقه هیچ جوابی نگیرد رها و دوباره فرستاده می‌شود.
- رفتار معامله‌ای ربات عوض نشده است و تأیید تازه (`confirm-live`) لازم نیست.
- English: `bitpin/news.py` - `build_news_messages` names today's date, asks for the month and year in every
  query, at least the crypto/US-macro and the Iran/rial searches, time_hint as YYYY-MM-DD (and no longer
  mentions the old 13:00 slot); `item_age_days` / `is_old_item` date a time hint (full dates, else the end of
  the month or year it names); `sanitize_reply(now=)` leaves out items older than `NEWS_MAX_ITEM_AGE_DAYS` (7)
  with a note (`OLD_ONLY_SUMMARY` when nothing is left); `_search_call(now=)` answers a reply without recent
  items once with `fresh_search_nudge` while search rounds are left; a brief without items is reused for
  `EMPTY_BRIEF_REUSE_MINUTES` (180) only and skips the after-HOLD gate; `_one_request` ends a streamed
  request without a response after `STREAM_HEADERS_SECONDS` (120; shared by the decision calls through
  `bitpin.llm.make_llm_transport`). Tests: `TestCurrentNews`, `TestNoAnswerLimit` in `tests/test_news.py`.

## 3.2.0 — «پنل دوزبانه» (2026-09-28)

- **پنل حالا دو زبان دارد: English و فارسی.** دکمهٔ زبان بالای هر صفحه (و صفحهٔ ورود) است و انتخاب شما یک سال در همان
  مرورگر می‌ماند؛ تا انتخاب نکرده‌اید، زبان مرورگر تعیین می‌کند. همهٔ متن‌ها، فرم تنظیمات معامله (برچسب‌ها، توضیح‌ها، واحدها،
  گزینه‌ها و پیام‌های خطا) و قواعد رمز عبور در هر دو زبان‌اند. گزارش فارسی کیمی همان‌طور که هست نشان داده می‌شود.
- **ظاهر تازه:** منوی کناری با آیکون (روی گوشی نوار افقی بالای صفحه)؛ کارت‌های عددی برای ارزش حساب، سود / زیان، افت از سقف (با
  نوار تا حد توقف)، هزینهٔ مدل و سفارش‌های باز؛ نوار وزن‌های هدف کیمی؛ وضعیت سرویس‌ها با برچسب رنگی؛ فرم تنظیمات معامله با
  فهرست بخش‌ها، دو ستون، کلید روشن/خاموش و نوار ذخیرهٔ ثابت پایین صفحه؛ حالت روشن و تیره مطابق تنظیم دستگاه. همچنان بدون
  JavaScript و بدون هیچ فایل بیرونی.
- رفتار ربات عوض نشده است؛ تأیید قبلی (`confirm-live`) معتبر می‌ماند.
- English: new `bitpin/panel_i18n.py` (the Persian catalog of every panel text, `tr()` / `N_()`, `Text(fa, en)`
  pairs, the request's language in a thread-local reset after every request, Accept-Language negotiation);
  `bitpin/panel_web.py` views rewritten (sidebar layout, inline SVG icons, KPI cards, `<progress>` bars), `GET
  /lang?to=&next=` sets `__Host-bplang` (Secure, HttpOnly, SameSite=Strict, one year; `next` limited to the panel's
  own pages); `bitpin/static/panel.css` replaces the inline stylesheet (served as `/static/panel.css?v=<hash>` with a
  long cache, as is `/static/icon.svg`; every other response stays `no-store`); `bitpin/panel_settings.py` labels,
  help texts, units, choices and form errors are `Text` pairs (`FieldError`); `panel_auth.password_problems(lang=)`;
  `deploy/lib.sh` refuses a staged copy without the stylesheet and `scripts/panel_server.py --check` warns about it.
  Tests: `tests/test_panel_i18n.py` (every text translated, nothing left over, placeholders and markup kept, the
  negotiation, the thread-local), `TestLanguages` in `tests/test_panel_web.py` (default, Accept-Language, cookie, the
  switch, no open redirect, no Persian interface text on English pages, the language reset after a crash).

## 3.1.1 — اصلاح ورود به پنل (2026-09-28)

- **ورود به پنل با خطای `403 Forbidden (origin)` رد می‌شد.** پنل به مرورگر گفته بود هیچ «ارجاع‌دهنده‌ای» نفرستد
  (`Referrer-Policy: no-referrer`)، و طبق استاندارد مرورگرها در این حالت با هر فرم `Origin: null` می‌فرستند که بررسی Origin
  پنل ردش می‌کرد. حالا `same-origin` است، و `Origin: null` فقط همراه با `Sec-Fetch-Site: same-origin` خود مرورگر پذیرفته
  می‌شود؛ توکن CSRF و کوکی SameSite=Strict مثل قبل بررسی می‌شوند.
- **صفحهٔ سیاه پشت VPN:** مسیری که بسته‌های بزرگ را گم می‌کند صفحهٔ ورود کوچک را می‌رساند ولی فایل ظاهر ۵ کیلوبایتی را نه.
  پنل حالا داده را در تکه‌های کوچک‌تر (`TCP_MAXSEG` 1200) می‌فرستد.
- English: `bitpin/panel_web.py` SECURITY_HEADERS Referrer-Policy `same-origin` (Fetch standard: under
  `no-referrer` a form POST carries `Origin: null`), `_origin_ok` accepts `null` only with `Sec-Fetch-Site:
  same-origin`; `scripts/panel_server.py` sets TCP_MAXSEG 1200 on the listening socket (PMTU black holes on VPN
  paths). Tests: a browser-like POST (Origin null + Sec-Fetch-Site) logs in; a cross-site one is still refused.

## 3.1.0 — «پنل مدیریت» (2026-09-28)

### خلاصهٔ فارسی

یک **پنل مدیریت وب (HTTPS)** که از اینترنت با نام کاربری، رمز قوی و کد ۶ رقمی باز می‌شود، و پشتیبانی از **OpenRouter** و هر سرویس
سازگار با OpenAI برای مدل تصمیم و مدل خبر. آنچه ربات خودش انجام می‌دهد عوض نشده است، پس **تأیید قبلی (`confirm-live`) معتبر
می‌ماند** و به‌روزرسانی ربات را با همان تنظیمات دوباره روشن می‌کند. راهنمای کامل: [docs/PANEL_FA.md](docs/PANEL_FA.md).

- **پنل** (خاموش تا وقتی `sudo bitpin-bot panel-setup` زده شود): داشبورد (وضعیت، ارزش حساب، افت، آخرین تصمیم Kimi با گزارش
  فارسی، موقعیت‌ها، هزینهٔ مدل)، **تنظیمات معامله** با فرم فارسی (ریسک، حد توقف، ساعت تصمیم، بیدارباش‌ها، بازارها، نردبان، خروج‌ها،
  کارمزدها، اخبار، بودجه)، **مدل‌ها و کلیدها** (Moonshot / OpenRouter / سازگار با OpenAI، فهرست مدل‌ها با قیمت)، ویرایشگر کامل JSON،
  **اعمال تنظیمات** (بررسی، توقف، `confirm-live` با عبارت تایپ‌شده، روشن کردن، سلامت)، **VPN** (تست، جایگزینی سرور با لینک
  `vless/vmess/trojan/ss`، برگشت خودکار اگر کار نکند)، لاگ‌ها و امنیت.
- **امنیت:** وب با کاربر بی‌دسترسی `bitpin-panel`؛ کارهای root فقط با یک کمک‌کار با فهرست بستهٔ دستورها. رمز فقط به‌صورت هش PBKDF2
  (۶۰۰ هزار دور)، کد ۶ رقمی TOTP، قفل بعد از ۵ خطا، کوکی امن، CSRF، سرآیندهای امنیتی. کلیدها فقط‌نوشتنی‌اند. هر کلید فقط به
  سکوی خودش فرستاده می‌شود و نشانی بیت‌پین از پنل عوض نمی‌شود. هر ورود و هر تغییر در تلگرام گزارش می‌شود.
- **OpenRouter:** کلید `OPENROUTER_API_KEY`، انتخاب هر مدل (مثلاً `moonshotai/kimi-k3`)، قیمت‌ها خودکار برای شمارندهٔ هزینه، و
  جست‌وجوی وب OpenRouter برای مرحلهٔ خبر.
- **خط فرمان:** `confirm-live --show` (فقط متن تأیید) و `confirm-live --typed-phrase` (عبارتی که مالک در پنل تایپ کرده)؛
  `panel-setup`، `panel-password`، `panel-totp`، `panel-status`.

### English detail

#### A. Management panel
- `scripts/panel_server.py` + `bitpin/panel_web.py` + `bitpin/panel_auth.py`: a stdlib HTTPS server (TLS 1.2+) run by
  `deploy/bitpin-bot-panel.service` as the user `bitpin-panel` (NoNewPrivileges, ProtectSystem=strict, no capabilities,
  port 8443). Login: username + PBKDF2-SHA256 (600000 iterations) password + optional RFC 6238 TOTP with replay
  protection; per-IP lock (5 failures / 15 min) and a global lock (30 / h); server-side sessions (`__Host-` cookie,
  Secure, HttpOnly, SameSite=Strict, 30 min idle, 12 h max); CSRF token and Origin check on every POST; CSP,
  HSTS, frame-ancestors none, nosniff, no-referrer, no-store; an audit log (`/var/lib/bitpin-bot-panel/audit.jsonl`).
  Pages: dashboard, trade settings, models and keys, JSON settings, apply, VPN, logs, security. Persian RTL.
- `bitpin/panel_settings.py`: the trade settings form (about 60 keys of config.json / kimi.json in 9 groups, Persian
  labels, percent display for fractions, Persian digits accepted). The helper accepts only these keys through the
  form, re-checks kind and bounds, writes only real changes (a no-op save never forces a new confirm-live) and never
  re-creates a section the owner switched off (`"news": null`).
- `scripts/panel_helper.py`: the root helper behind `deploy/bitpin-bot-panel-helper.socket` (Accept=yes,
  root:bitpin-panel 0660, SO_PEERCRED checked) - one JSON request per connection from a closed command list. Every
  settings write runs `deploy/apply_profile.validate` (the bot's own checks) plus the endpoint rule first: config.json
  `base_url` must be `https://api.bitpin.org`, and an LLM stage's `base_url` must be the platform of its
  `api_key_env` (KIMI_API_KEY -> Moonshot, OPENROUTER_API_KEY -> openrouter.ai, LLM_API_KEY -> the host saved with
  it as LLM_API_HOST). Backups (`*.before-panel`, 30 per file) before every write. Secrets are write-only.
  `apply_live`: `confirm-live --show` must pass before the bot is stopped; then stop, `confirm-live --typed-phrase`,
  reset-failed, start, health. `service` refuses to start a bot whose settings are not confirmed.
- `bitpin/vpn.py`: share links (vmess, vless incl. REALITY, trojan, shadowsocks SIP002 and legacy) -> an xray
  outbound that replaces the proxy outbound in place (inbounds and routing kept); masked summaries; `xray run -test`
  before a write; an end-to-end proxy test (HTTP CONNECT / SOCKS5 + TLS handshake) after the tunnel restart, with an
  automatic rollback when the bot could not reach its LLM host and Telegram. New or changed inbounds must listen on
  the loopback.
- `bitpin/notify.py`: `panel_event.*.json` relay files (written by the helper) become Telegram alerts: panel login,
  login lock, and every change made through the panel (settings, model, key name, VPN, password, 2FA, restarts).
- `scripts/panel_setup.py` (`sudo bitpin-bot panel-setup|panel-password|panel-totp|panel-status`): the system user,
  `/etc/bitpin-bot-panel` (root:bitpin-panel 0750), username / password / TOTP questions, a self-signed EC certificate
  (825 days) with its SHA-256 fingerprint, the units enabled. Opening the firewall port is left to the owner.
- Deploy kit: `PANEL_UNITS` are installed by install.sh / update.sh but never enabled there; a running panel is
  restarted after an update; a rollback to a tree without them removes them; uninstall removes them (purge: the user
  and its directories).

#### B. LLM providers
- `llm.provider` / `news.provider` (`auto` | `moonshot` | `openrouter` | `openai`, auto from the base_url host) and
  `news.api_key_env`. OpenRouter: `reasoning: {effort}`, `usage: {include: true}`, `X-Title`, SSE comments ignored,
  `delta.reasoning` / `reasoning_details` collected, HTTP 402 classified as `llm_quota`, error bodies parsed.
  `list_models_detailed()` returns prices per million tokens. The news stage on OpenRouter uses the web plugin
  instead of Moonshot's `$web_search`. The Kimi model rules strip a `vendor/` prefix.

#### C. Command line
- `run_bot.py confirm-live --show` (the summary only, no terminal, nothing written) and `--typed-phrase PHRASE`
  (no terminal); `--check`, `--show` and `--typed-phrase` are mutually exclusive. LIVE_CONFIRMED records
  `confirmed_how`. `bitpin-bot confirm-live --show` is allowed while the bot runs.

#### D. Update path
- A MINOR release: `DIGEST_VERSION` is unchanged, so the existing confirmation stays valid. The usual update:
  `sudo bash deploy/update.sh` from the new tree, then `sudo bitpin-bot health`. The panel stays off until
  `sudo bitpin-bot panel-setup`.

---

## 3.0.0 — «یک سال» (2026-09-26)

### خلاصهٔ فارسی

مسابقه یک‌ماهه نبود؛ **یک سال** است (از ۳۰ شهریور ۱۴۰۵ / 2026-09-21 20:30 UTC تا ۳۰ شهریور ۱۴۰۶ / 2027-09-21 20:30 UTC). نسخهٔ ۲ با تاریخ‌های مهر ۱۴۰۵ از ۲۵ مهر هیچ خریدی نمی‌کرد، ۲۹ مهر همه‌چیز را به USDT می‌برد و ۱۱ ماه در حالت «پایانی» می‌ماند. نسخهٔ ۳ کل ربات را برای افق یک‌ساله بازتنظیم می‌کند، **با پذیرش ریسک** و با همان تصمیم‌گیر (Kimi k3). نتیجهٔ مطالعهٔ یک‌سالهٔ Y1 روی ۴۵۰ پنجرهٔ ۳۶۵ روزهٔ بیت‌پین: تنظیم نسخهٔ ۲ (stop ثابت −۱۲٪، halt ۳۰٪، نگه‌داری حداکثر ۷ روز) به‌طور انتظاری برابر «هیچ کاری نکردن» بود و در ۱۰۰٪ پنجره‌ها halt می‌خورد.

چه چیزی برای مالک عوض می‌شود:

- **تاریخ‌ها:** پایان مسابقه 2027-09-21؛ ممنوعیت ورود جدید از ۲۵ شهریور ۱۴۰۶ (2027-09-16 13:00 تهران)؛ تصمیم پایانی ۲۹ شهریور ۱۴۰۶ (2027-09-20 13:00 تهران). اگر تاریخ‌های `brain.endgame` و `context.competition_end_utc` بیش از ۱۴ روز با هم نخوانند، ربات هنگام شروع هشدار می‌دهد.
- **افق نگه‌داری:** حداکثر ۷۲۰ ساعت (۳۰ روز) به‌جای ۱۶۸؛ افق طرح‌های Kimi ۲۴ تا ۷۲۰ ساعت.
- **حد ضرر پیش‌فرض −۱۲٪ حذف شد.** حد ضرر فقط وقتی هست که Kimi برای همان کوین صریحاً `stop_pct` بدهد (۵ تا ۴۰٪). هدف سود و بیدارباش‌ها مثل قبل.
- **توقف در افت (halt) از ۳۰٪ به ۵۰٪** و سقف مرجعِ آن به‌جای «بالاترین ارزش تاریخی»، بالاترین ارزش **۹۰ روز اخیر** است، تا یک سال گران شدن تتر ربات را در یک halt همیشگی گیر نیندازد.
- **نردبان ریزش:** فقط سطح −۲۰٪ با اندازهٔ ۰٫۲۵ سرمایه؛ بیدارباش حرکت کوین نگه‌داشته‌شده از ±۸٪ به ±۱۲٪؛ خبر حداکثر ۲ بار در روز و فقط وقتی تصمیم قبلی «نگه‌دار» ساده نبوده (یا خلاصهٔ خبر بیش از ۴۸ ساعت کهنه است).
- **دارایی‌های واقعی (RWA):** توکن‌های طلا، نقره، نفت، گاز، مس، سهام و ETF آمریکا در فهرست مجاز، هرکدام با کلاس دارایی. برای سهام/ETF/نفت/گاز، خرید فقط در ساعت بازار آمریکا (دوشنبه تا جمعه ۱۳:۳۰ تا ۲۰:۰۰ UTC) و با اسپرد زیر ۱٪؛ فروش همیشه آزاد.
- **prompt جدید** (`bitpin/prompt_template.txt`): معیار سود انتظاری بعد از هزینه (`ev_pct >= 0`)، احتمال پایه از خود داده (`p0 = l/(g+l)`)، سقف تغییر احتمال، مورد نزولی و مورد USDT برای هر ورود، خوشه‌ها (کریپتو / کریپتو-بتا / طلا / نقره / نفت / سهام آمریکا / اوراق). اعتبارسنج جدید تحلیل Kimi هفتهٔ اول فقط یادداشت می‌نویسد (`brain.analysis_policy: "off"`)؛ مالک بعداً به `"block"` می‌برد.
- **هزینهٔ Kimi:** `llm.reasoning_effort` (پیش‌فرض `"high"`)، فایل استدلال هر تصمیم در `decisions/<time>.reasoning.txt` (بدون کلید، ۶۰ روز)، شمارندهٔ هزینهٔ دلاری در `llm_spend.json`، و هشدار فارسی وقتی اعتبار Moonshot تمام شده.
- **اطلاع‌رسان:** ضربان بر پایهٔ چرخه‌های موفق (نه mtime لاگ)، هشدار «ربات روشن است ولی چرخه‌ها خطا می‌دهند»، «۳۰ ساعت بدون تصمیم معتبر»، تشخیص قطع تونل، خط روزانه (سرمایه، افت، هزینهٔ LLM)، خلاصهٔ هفتگی جمعه ۲۰:۰۰ تهران.
- **سرور:** خروج دو سرویس معامله‌گر ربات از needrestart، هشدار تلگرام روی خروج با کد ۷۸، logrotate، پشتیبان روزانهٔ state (۱۴ نسخه)؛ بدون هیچ تنظیم سراسری روی سرور. نصب/به‌روزرسانی از `git clone` مستند شد.
- **اضافه‌های ۲۰۲۶-۰۹-۲۷:** فهرست ۵۳ بازار مالک (با ۱۵ توکن نفت، گاز، مس، سهام و ETF) در `kimi.example.json`، تا `apply_profile.py` آن‌ها را حذف نکند؛ ربات پیش از خرید توکن‌های بازار آمریکا اسپرد بالای ۱٪ را هم رد می‌کند؛ Kimi با `stop_pct` = `0` می‌تواند حد ضرری را که از قبل روی پوزیشن هست بردارد (فقط در تصمیمی که اجازهٔ خرید دارد).
- **ساعت تصمیم روزانه ۱۹:۰۰ تهران شد** (قبلاً ۱۳:۰۰): ۱۳:۰۰ تهران همیشه بیرون از ساعت بازار آمریکاست و توکن‌های سهام و نفت هیچ‌وقت خریدنی نبودند؛ ۱۹:۰۰ در تابستان و زمستان آمریکا داخل بازار است. ساعت تابستانی آمریکا هم حساب می‌شود.
- **این نسخه باید دوباره `confirm-live` شود** (آنچه ربات خودش انجام می‌دهد عوض شده: خروج‌ها، نردبان، halt). مسیر به‌روزرسانی در README و `docs/DEPLOY_FA.md`.

### English detail, by spec section

Evidence: the study folders named below (`arch_review/`, `year_review/`, `prompt_review/`, `rwa_scan.json`)
stayed in the build workspace; their reports were copied into the repo on 2026-09-27:
`docs/reviews/2026-09-26_architecture/` (26-day architecture review), `docs/reviews/2026-09-26_one_year/`
(one-year replan, `ROADMAP_fa.md`), `docs/reviews/2026-09-26_prompt/`, `docs/dev_notes/v3/prompt_package/` (the
new prompt package, = `prompt_review/final/`) and `research/05_rwa_scan/` (RWA token scan). Build state and next
steps: `docs/dev_notes/HANDOFF.md`.

#### A. Horizon and competition length (`bitpin/brain.py`, `kimi.example.json`, docs)

- A1 `MAX_HOLD_HOURS` 168 -> 720 (30 days). Plan `horizon_hours` clamp 6..168 -> 24..720; exits `max_hold_hours`
  clamp 1..168 -> 1..720. The endgame cap is unchanged: no hold runs past `max_hold_cap_at`. Why: Y1 - every avoided
  round trip on the invested part is worth about 0.5 pp, and a 30-day hold beat a 7-day one on the 365-day windows.
- A2 The -12% code stop is no longer a default. `STOP_PCT_DEFAULT` is `None`: a position has a stop only when Kimi
  sets `stop_pct` in its exits (clamped 5..40). Ladder-fill positions keep their own target rule (half the 48 h
  drop) and get no default stop either. Every text that named the default (runner exits, prompt, banner,
  confirm-live digest, notifier) now says "no default stop". Why: Y1 - every fixed stop tested was net negative
  over a year (65% coins without a stop 1.143 mean vs 0.929 with the weekly-reset -12% stop).
- A3 "one-month trading competition" -> "one-year trading competition" in the prompt; `days_left` still computed.
  Defaults: `context.competition_end_utc` `2027-09-21T20:30:00Z`, `brain.endgame.no_new_entries_at`
  `2027-09-16T13:00:00+03:30`, `final_at` / `max_hold_cap_at` `2027-09-20T13:00:00+03:30`. New startup check: a
  warning when the endgame dates and `competition_end_utc` disagree by more than 14 days. `deploy/apply_profile.py`
  carries the 2027 values. Docs updated (`DEPLOY_FA.md`, `STRATEGY_KNOWLEDGE.md`, `README.md`).

#### B. LLM client (`bitpin/llm.py`, `bitpin/news.py`, `bitpin/brain.py`)

- B1 New optional `llm.reasoning_effort`: `null | "low" | "high" | "max"`, sent top-level in the request body when
  set, refused together with any `thinking` key; `validate_llm_config` accepts it. `kimi.example.json` ships
  `"high"` (the owner's cost tier, Y3: 2.7x fewer reasoning tokens for about one quality point). A separate
  `brain.slot_reasoning_effort` (default `null` = same) lets the daily slot use `"max"`.
- B2 `reasoning_content` deltas from the stream are captured into `res["reasoning"]`; the brain writes
  `state_dir/decisions/<decided_at_utc>.reasoning.txt` after secret redaction. Never echoed into a prompt, never
  logged to journald, not in the notifier's allowlist. Files older than 60 days are deleted at startup.
- B3 News stage: the kimi-k2.6 brief runs only when the previous decision was not a plain HOLD or the brief is
  older than 48 h (`news.after_hold_only`, default `true`), plus on every wake-up as before. `cache_minutes` stays
  1380. Why: Y3 - about 0.6 briefs/day instead of 1 (about 40% fewer news calls).
- B4 LLM cost meter: USD per call and per UTC day from `kimi_usage.jsonl` with price keys
  `llm.price_in_per_m`, `llm.price_out_per_m`, `llm.price_cached_in_per_m` (k3 defaults $3 / $15 / $0.30 per M;
  the news model as configured, default $1 / $4). Written to `state_dir/llm_spend.json`
  `{today_usd, month_usd, total_usd}`; the notifier shows a daily line. Alert after 2 consecutive `llm_quota` / 429
  quota errors (Persian: the Moonshot credit is exhausted; derisk in N hours).

#### C. Context (`bitpin/analysis.py`)

- C1 Per symbol (USDT terms): `sig_d` (daily sigma from the last 168 hourly log returns x sqrt(24), %), `d30h`
  (px / max close of the last 720 h - 1, %), `pos30` (position in the 30-day range 0..1), `beta_btc` (720 h
  regression vs BTC_IRT/USDT_IRT), `corr_btc_30d`, `rsi1d`. Removed: `rsi1h`, `ret_irt` for coins, the 1 h / 4 h
  return entries, `don_pos`, `flow`, `depth2_m` (`depth1` kept). Legend updated; prompt size target <= 22k chars
  with 53 symbols. Why: `arch_review/A1-context/features_eval.json` - the removed fields carried no holdout signal.
- C2 Portfolio block: `coin_share`, weighted `beta_btc`, average pairwise 30-day correlation of held coins,
  `sigma7_equity_pct = sqrt(w'Cw) * sqrt(7)`, `effective_bets = (sum w)^2 / (w'Cw)`, `loss_if_all_stops_pct`
  (positions with a stop only), cumulative fees + slippage since the competition start (toman and % of equity),
  7-day turnover. The drawdown block adds the distance to the halt.
- C3 Asset classes from a static map: `crypto` (default), `gold` (PAXG, XAUT, GLDON), `silver` (SLVON), `oil`
  (USOON), `gas` (UNGON), `copper` (COPXON), `us_stock` (xStocks ...X, Ondo ...ON, bStocks ...B), `us_etf` (SPYON,
  QQQON, TLTON, AGGON, IEFAON, SMHB, DRAMB), `bond` (TLTON, AGGON); COINX / CRCLX / MSTRON / HOODX flagged
  `crypto_beta`. For `us_stock` / `us_etf` / `oil` / `gas`: `us_session` (open / closed from Mon-Fri 13:30-20:00
  UTC, no holiday calendar), `quote_noise_pct` (std of hourly log returns, 7 d) and the blocked reason
  `us market closed` when the session is closed OR the spread is above 1%: Kimi may not increase them, the code
  refuses buys, sells are allowed. The same trading-hours guard runs in the runner before any buy on those classes.
- C4 Static base-rate table by setup and asset class in `docs/STRATEGY_KNOWLEDGE.md` (rows B1..B12 with condition,
  horizon, sample, TRAIN / HOLDOUT numbers) from `arch_review/E5-base-rates`, `A1-context/features_eval.json`,
  `year_review/Y1` (coin share, stops, halt, the 84-day trend state) and `year_review/Y5` (gold / RWA). The 84-day
  BTC/USDT trend state (above / below its 84-day-ago close, and the 63-day one) is a context field with its base
  rate line; there is NO hard cap in code (the owner accepts the risk; Kimi decides).
- C5 `context.rule_signals` default `false` (the rejected-strategies block is dropped); `context.matches` `false`.

#### D. Prompt (`bitpin/prompt_template.txt`, `bitpin/brain.py`, `kimi.example.json`, `docs/STRATEGY_KNOWLEDGE.md`)

- D1 The authored prompt is replaced by `prompt_review/final/system_prompt.txt` rendered from placeholders
  (`{{END}}`, `{{MECHANICS}}`, `{{KNOWLEDGE}}`, `{{PLANS_SCHEMA}}`, `{{EXITS_SCHEMA}}`, ...), shipped as
  `bitpin/prompt_template.txt` (added to `deploy/lib.sh`'s include list). v3 adaptations: one-year wording,
  horizons 24..720 h, "a coin opened now runs at most to the FINAL decision" kept, `headroom_pct = halt_pct -
  |drawdown| - 5` with the configured halt (50) instead of the literal 30. THE HURDLE for the aggressive objective:
  pass = `ev_pct >= 0` after cost (not `>= c`); p shift caps +0.15 (positive row) / +0.20 (dated coin-specific
  event), hard cap 0.80; `p0 = l/(g+l)`, citations, `bear`, `usdt_case`, `would_flip`, clusters (crypto /
  crypto_beta / gold / silver / oil / us_equity / bond), `bad_move` per cluster (crypto 20, crypto_other 30,
  gold / silver 10, oil 15, us_equity 12). RWA rules: judge stock / oil tokens by the underlying's outlook; never
  increase while `us_session` is closed or the symbol is blocked; gold as an alternative to the USDT sleeve is
  allowed when its row supports it; "size decisively; concentration allowed; USDT_IRT is the benchmark, not a
  refuge". Every pinned sentence the tests check is kept (`tests/test_prompt_template.py`).
- D2 Validator phase 2 (`parse_analysis`) per `code_changes.md` section 3, policy `brain.analysis_policy` default
  `"off"` for the first week (notes only); the owner switches to `"block"` later. Thresholds are constants.
- D3 `extra_instructions` (the `AGGRESSIVE_STYLE_INSTRUCTIONS` constant, `kimi.example.json`, `apply_profile.py`):
  `prompt_review/final/extra_instructions.txt` merged with the ASSET CLASSES note live on the server; one-year
  sentence; every pinned phrase kept.
- D4 User-message tail per `code_changes.md` section 2; the validation-retry note is appended.
- D5 `report_fa` limit 800 chars; the notifier renders the analysis numbers per coin (p / ev / cost / verdict)
  under the Persian report.

#### E. Ladder / risk config defaults (`config.example.json`, `deploy/apply_profile.py`, docs)

- E1 `ladder.levels_pct [-20]`, `ladder.size_frac 0.25`, `risk.min_order_usdt 1.05`, `risk.max_drawdown 0.50`
  (halt), `risk.drawdown_action halt`, `brain.held_move_pct 12`, `news.max_calls_per_day 2`. A concentrate rule
  in code is not needed (verified equal to config-only, `year_review/V2-rank4-lean-config`).
- E2 The halt is still measured on toman equity but against a rolling 90-day high-water mark instead of the
  all-time one (`risk.hwm_window_days 90`; `0` = all-time), so a year of toman appreciation cannot trap the bot in a
  permanent halt. Why: Y1 / `V2-halt50` - the 50% halt never fired on any 365-day window, the 30% one always did.

#### F. Notifier and ops (`bitpin/notify.py`, `deploy/`)

- F1 Heartbeat keyed on successful cycles (the runner state's `last_cycle.time` with status ok, not the log's mtime); alerts "running but
  cycles failing" after 2 h without a successful cycle and "no valid decision for 30 h"; tunnel-down detection
  (Telegram send failing 3 times AND the bot's Kimi errors are connection errors -> one Persian alert once the
  tunnel is back, with the outage window); the `llm_quota` alert (B4); a daily line with equity, drawdown and LLM
  spend.
- F2 Weekly Persian summary (Friday 20:00 Tehran): equity vs the USDT benchmark since the start, positions, fees,
  LLM spend, alert count.
- F3 `deploy/`: `/etc/needrestart/conf.d/bitpin-bot.conf` excludes the two trading units from needrestart; a
  systemd `OnFailure` unit (run as user bitpin) sends a Telegram alert on exit 78 through the notifier's read-only
  relay path; logrotate for `/var/lib/bitpin-bot/bot_live.log` (copytruncate, 20 MB x 5); a daily
  state backup timer to `/var/backups/bitpin-bot/state-<date>.tgz` (keep 14). `install.sh` / `update.sh` install
  these idempotently; `uninstall.sh` removes them. An existing tunnel's own drop-in is left untouched.
- F4 `deploy/lib.sh` `stage_code`: `research/`, `docs/dev_notes/` and `docs/reviews/` are no longer copied to
  `/opt/bitpin-bot`; `bitpin/prompt_template.txt` is included.
- F5 `docs/DEPLOY_FA.md`: a `git clone` install / update section, the monthly checklist (Persian, from
  `year_review/Y2`), the alerts explained. `docs/TELEGRAM_FA.md`: the new messages.

#### Additions of 2026-09-27 (integration check)

- The example universe is the owner's live list of 53 markets (`kimi.example.json` `brain.allowed_symbols` =
  `context.universe`: USDT_IRT, 37 coins and the 15 RWA tokens added on 2026-09-26) and `news.extra_topics` carries
  the owner's oil / gold / US-stock / Iran topics: `deploy/apply_profile.py` SETS the symbol lists from the example,
  so a 38-symbol example would have dropped the 15 tokens on the server. The context with 53 symbols measured
  15,200 characters on public data (limit 26,000, nothing truncated).
- The runner refuses a buy of a session-bound token (US stock / ETF / oil / gas) whose live spread is above
  `analysis.RWA_MAX_SPREAD_PCT` (1%) or whose book has an empty side (`Runner._spread_blocked`), right before the
  order and next to the US-session check; before, only the context's `blocked` marker enforced the cap.
- `stop_pct` 0 in the reply's exits means NO stop: it removes a stop in force (positions opened under v2 keep the
  old -12% stop until then) in the modes that may buy; the no-buy modes keep it. Before, 0 was clamped to a 5% stop
  and there was no way to remove a stop. The exits schema in the prompt says so.
- Security review: the failure relay `bitpin-bot-failed@.service` runs as user bitpin with no capability (it
  was root inside the notifier's directory, which bitpin owns); the needrestart exclusion covers only the two
  trading units and the monthly checklist says when to restart the bot after security updates; the journald
  drop-in (a system-wide 300 MB cap that also cut the log retention of other services on a shared server) is no longer
  shipped; reasoning files are created 0600 without following links; the credential redaction of URLs covers any
  scheme (socks5://user:pass@...); the unused second reasoning writer in `bitpin/llm.py` is gone.
- Money review: the daily slot moves from 13:00 to 19:00 Tehran (`brain.decision_times_local` ["19:00"] =
  15:30 UTC): at 13:00 (09:30 UTC) every session-bound token was "us market closed", so the daily decision could
  never buy the tokenized stocks / ETFs / oil the owner added; 19:00 is inside the session in US summer and winter
  time. The US session follows US daylight saving (09:30-16:00 New York = 13:30-20:00 UTC in summer, 14:30-21:00
  UTC in winter). GLDON and SLVON (Ondo tokens of US funds) are session-bound like the stock tokens. The runner's
  decision clamp checks the session at the time of execution (after the Kimi call) and the 1% spread too, so no
  funding sale runs for a buy it will refuse. The analysis check: shape errors of the block no longer reject the
  whole reply under "off" (and on the last "block" attempt they block only the rising coins); a field name inside
  an expression ("2 x atr4h_pct=3.2") is not a citation and thousands separators are read ("80,000"). The
  after-HOLD news gate applies to the clock slots only: every wake-up refreshes a brief older than the cache as
  in v2. `risk.hwm_window_days` accepts a decimal number (90.0).
- Consistency: IEFAON is in the us_equity cluster of the prompt (the code classes it `us_etf`); stale v2 docstrings
  (plan horizon 6..168, default exits) corrected; the Persian guides list the new settings, the reasoning files,
  `llm_spend.json`, the ordered v3 update commands and the US-market Telegram message; the news budget is 2 a day
  everywhere.

#### G. Tests, release, versioning

- G1 `bitpin/__init__.py` `__version__ = "3.0.0"`; the startup banner prints it; this CHANGELOG.
- G2 Full suite green on Python 3.14 and 3.7, new tests for every item above. The prompt checker script
  (`prompt_review/final/assemble_and_check.py`) became `tests/test_prompt_template.py`: it renders every
  profile x web x plans_on variant and asserts that no `{{` remains and every pinned sentence exists.
- G3 Release: the tarball is built the same way as before (`docs/DEPLOY_FA.md` step 1), named
  `scratch/bitpin-bot-release-v3.tgz`. Update path on the server, in this order:
  1. `scp scratch/bitpin-bot-release-v3.tgz USER@SERVER:~/` and unpack it into an EMPTY directory
     (`rm -rf ~/bitpin-bot-src-new && mkdir ~/bitpin-bot-src-new && tar -xzf ~/bitpin-bot-release-v3.tgz -C ~/bitpin-bot-src-new`),
     or `git pull` in a clone outside `/opt/bitpin-bot` with a clean `git status --short`.
  2. `sudo systemctl stop bitpin-bot` - v3 changes what the bot does on its own (exits, ladder, halt), so
     `update.sh` refuses to swap the code under a live bot that still runs on the v2 confirmation.
  3. `cd ~/bitpin-bot-src-new && sudo bash deploy/update.sh` (tests, config check, staged swap, unit files).
  4. `sudo python3 /opt/bitpin-bot/deploy/apply_profile.py` for the v3 settings (backs up `config.json` /
     `kimi.json` first; the undo command is printed), then `sudo bitpin-bot check` and
     `sudo bitpin-bot kimi-check --news`.
  5. `sudo bitpin-bot confirm-live`, then `sudo systemctl start bitpin-bot`; verify with `sudo bitpin-bot health`
     and the banner in `sudo bitpin-bot logs` (it prints `version 3.0.0`).
  Rollback: `sudo bash /opt/bitpin-bot/deploy/update.sh --rollback` after restoring the `*.before-profile` copies
  from `/var/backups/bitpin-bot` (the v2 code rejects v3 keys such as `risk.hwm_window_days`).

Housekeeping: `tests/test_release_review.py` no longer exercises the `moonshot-v1-*` ids (retired by Moonshot on
2026-08-31); the non-thinking side of the set-model tests uses `kimi-k2.7-code` / `kimi-k2.7-code-highspeed`,
two ids the server's key listed on 2026-09-22. `scripts/check_balance.py` docstring rewritten (usage from the
project root, what is printed, why it is a diagnostic and not a loop).

---

## 2.x — daily decision + crash ladder + plan memory (2026-09-24, live on the server)

The version this file starts from: one Kimi decision a day at 13:00 Tehran (kimi-k3, JSON), a kimi-k2.6 news
brief with `$web_search`, code-placed crash-ladder bids at -20% / -25% on COIN_USDT, code exits (-12% stop, target,
168 h max hold), plan memory per entry, the pump guard, the 30% drawdown halt, the Telegram notifier as a separate
read-only service, and the deploy kit (`install.sh` / `update.sh` with staging, tests and auto-rollback). 993 tests.
