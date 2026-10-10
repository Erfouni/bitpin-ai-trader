"""Offline Persian daily research report; public data, no scripts or remote resources."""
import base64
from datetime import datetime, timedelta, timezone
import html
from pathlib import Path

from .defi_daily import DAY, METRICS, atomic_text

LABELS = {"tvl": "ارزش قفل‌شده · TVL", "dex_volume": "حجم معاملات DEX",
          "stablecoin_supply": "عرضهٔ استیبل‌کوین‌های دلاری", "fees": "کارمزد پرداختی کاربران",
          "revenue": "درآمد باقی‌ماندهٔ پروتکل‌ها", "open_interest": "ارزش قراردادهای باز · OI"}
NOTES = {"tvl": "تغییر قیمت دارایی‌ها هم این مقدار را تغییر می‌دهد؛ معادل ورود خالص پول نیست.",
         "dex_volume": "فعالیت معاملات نقدی؛ افزایش حجم، جهت خرید یا فروش را مشخص نمی‌کند.",
         "stablecoin_supply": "فقط تعداد توکن‌های وابسته به دلار؛ شامل تغییر قیمت و ارزهای دیگر نمی‌شود.",
         "fees": "هزینهٔ استفاده از پروتکل‌های تحت پوشش؛ این مبلغ، سود دارندگان توکن نیست.",
         "revenue": "سهم باقی‌مانده پس از پرداخت به تأمین‌کنندگان خدمات؛ معادل سود خالص نیست.",
         "open_interest": "ارزش موقعیت‌های باز صرافی‌های غیرمتمرکز؛ اندازهٔ مواجهه است، نه جهت بازار."}
STATUS = {"ok": "قابل بررسی", "missing": "داده موجود نیست", "error": "خطای دریافت", "stale": "داده کهنه"}


def esc(value):
    return html.escape(str(value), quote=True)


def number(value):
    if value is None:
        return "—"
    for scale, label in ((1e9, "میلیارد"), (1e6, "میلیون"), (1e3, "هزار")):
        if abs(value) >= scale:
            return '<bdi>%.2f</bdi> <small>%s</small>' % (value / scale, label)
    return '<bdi>%.2f</bdi>' % value


def percent(value):
    return "—" if value is None else '<bdi>%+.1f%%</bdi>' % value


def badge(status):
    label = STATUS.get(status, "ناموجود")
    return '<span class="badge %s">%s</span>' % ("ok" if status == "ok" else "warn", label)


def sparkline(series):
    if not series:
        return '<div class="no-chart">تاریخچهٔ کافی برای نمودار موجود نیست</div>'
    end = series[-1][0]
    start = end - 29 * DAY
    rows = [row for row in series if start <= row[0] <= end]
    if len(rows) < 2:
        return '<div class="no-chart">تاریخچهٔ کافی برای نمودار موجود نیست</div>'
    values = [r[1] for r in rows]
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    groups = [[]]
    previous = None
    for stamp, value in rows:
        if previous is not None and stamp - previous != DAY:
            groups.append([])
        groups[-1].append(((stamp - start) * 280 / (29 * DAY), 55 - (value - lo) * 45 / span))
        previous = stamp
    shapes = []
    for group in groups:
        if len(group) == 1:
            shapes.append('<circle cx="%.2f" cy="%.2f" r="2.5" fill="#74dfce"/>' % group[0])
        else:
            points = " ".join("%.2f,%.2f" % point for point in group)
            shapes.append('<polyline points="%s" fill="none" stroke="#74dfce" stroke-width="2.5" '
                          'vector-effect="non-scaling-stroke"/>' % points)
    return ('<svg viewBox="0 0 280 65" role="img" aria-label="روند مشاهده‌های روزانه؛ حداکثر سی روز" '
            'preserveAspectRatio="none"><path d="M0 60H280" stroke="#2b4053"/>%s</svg>') % "".join(shapes)


def render_report(snapshot):
    sources = snapshot["sources"]
    tehran = timezone(timedelta(hours=3, minutes=30))
    stamp = datetime.fromtimestamp(snapshot["generated_at"], tehran).strftime("%Y-%m-%d · %H:%M")
    good = sum(row["status"] == "ok" for row in sources.values())
    cards = []
    for metric in METRICS:
        row = sources.get(metric + ":global", {"status": "missing"})
        f = row.get("features", {}) if row["status"] == "ok" else {}
        unit = "واحد توکن دلاری" if metric == "stablecoin_supply" else "دلار آمریکا"
        flow = f.get("kind") == "daily_flow"
        left_label = "میانگین هفت روز" if flow else "تغییر هفت‌روزه"
        left = number(f.get("mean_7d")) if flow else percent(f.get("change_7d_pct"))
        right_label = "هفته نسبت به هفتهٔ قبل" if flow else "تغییر سی‌روزه"
        right = percent(f.get("week_over_week_pct") if flow else f.get("change_30d_pct"))
        cards.append('<article class="metric"><div class="card-top"><h2>%s</h2>%s</div>'
                     '<div class="value">%s</div><div class="unit">%s · روز UTC: <bdi>%s</bdi></div>'
                     '%s<div class="comparisons"><div><span>%s</span><strong>%s</strong></div>'
                     '<div><span>%s</span><strong>%s</strong></div></div><p class="note">%s</p></article>' %
                     (LABELS[metric], badge(row["status"]), number(f.get("value")), unit,
                      esc(f.get("date_utc", "—")), sparkline(f.get("series", [])), left_label,
                      left, right_label, right, NOTES[metric]))
    chains = sorted(set(s["scope"] for s in sources.values() if s["scope"] != "global"))
    rows = []
    for chain in chains:
        cells = []
        for metric in ("tvl", "dex_volume", "stablecoin_supply"):
            row = sources.get(metric + ":" + chain, {"status": "missing"})
            f = row.get("features", {}) if row["status"] == "ok" else {}
            val = f.get("mean_7d") if metric == "dex_volume" else f.get("value")
            change = f.get("week_over_week_pct") if metric == "dex_volume" else f.get("change_7d_pct")
            cells.append('<td><strong>%s</strong><span class="cell-detail">%s · <bdi>%s</bdi></span></td>' %
                         (number(val), percent(change) if row["status"] == "ok" else badge(row["status"]),
                          esc(f.get("date_utc", "—"))))
        rows.append('<tr><th scope="row"><bdi>%s</bdi></th>%s</tr>' % (esc(chain), "".join(cells)))
    audit = []
    for row in sources.values():
        f = row.get("features", {})
        age = row.get("receipt_age_seconds")
        audit.append('<tr><td>%s / <bdi>%s</bdi></td><td>%s</td><td><bdi>%s</bdi></td>'
                     '<td>%s</td><td><bdi>%s / 7</bdi></td><td><a href="%s" rel="noreferrer">API عمومی ↗</a></td></tr>' %
                     (LABELS.get(row["metric"], esc(row["metric"])), esc(row["scope"]), badge(row["status"]),
                      esc(f.get("date_utc", "—")), ("%.0f دقیقه" % (age / 60)) if age is not None else "—",
                      esc(f.get("coverage_7d", "—")), esc(row["url"])))
    font_file = Path(__file__).parent / "static" / "fonts" / "Vazirmatn-Regular.woff2"
    font = ("@font-face{font-family:Vazirmatn;src:url(data:font/woff2;base64," +
            base64.b64encode(font_file.read_bytes()).decode("ascii") + ") format('woff2');font-display:swap}") \
        if font_file.is_file() else ""
    template = '''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'">
<title>رصد روزانهٔ DeFi | آزمایش بیت‌پین</title><style>__FONT__
:root{color-scheme:dark;--bg:#09131f;--card:#112333;--ink:#eff6fc;--muted:#adbdcb;--line:#294052;--accent:#74dfce}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 90% 0%,#153746 0,transparent 45%),var(--bg);color:var(--ink);font:14px/1.85 Vazirmatn,Tahoma,sans-serif}
main{max-width:1240px;margin:auto;padding:36px 28px 64px}header{padding:8px 0 20px}.brand{font:12px/1.6 ui-monospace,monospace;color:var(--accent);letter-spacing:.15em}
.headline{display:flex;justify-content:space-between;gap:24px;align-items:center}h1{font-size:32px;margin:12px 0 6px;font-weight:600}h2{font-size:15px;margin:0;font-weight:500}p{margin:8px 0}.muted,.unit,.note{color:var(--muted)}
.mode{padding:8px 16px;border:1px solid #4c8f83;border-radius:12px;color:#a4eedf;white-space:nowrap}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:22px 0}
.summary>div{border-top:1px solid #365064;padding-top:14px}.summary strong{display:block;font-size:24px;font-weight:500}.summary span{color:var(--muted);font-size:12px}
.notice{display:flex;gap:12px;padding:16px 20px;background:#173b39;border:1px solid #2a6560;border-radius:14px;color:#d3eee8;font-size:13px}.notice b{white-space:nowrap}
.section-title{display:flex;justify-content:space-between;align-items:baseline;margin:26px 0 14px;gap:12px}.section-title h2{font-size:20px}.section-title span{font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.metric{background:linear-gradient(145deg,#142a3b,#10202f);border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:0 8px 28px #0002}
.card-top{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}.badge{display:inline-block;border-radius:20px;font-size:10px;padding:2px 8px;white-space:nowrap;border:1px solid #397363;color:#a5edda;background:#153c33}.badge.warn{border-color:#8a744b;color:#f1dba7;background:#3a3121}
.value{font-size:30px;line-height:1.6;margin:16px 0 0;font-weight:500}.value small{font-size:17px}.unit{font-size:10px}.metric svg{display:block;width:100%;height:68px;margin:18px 0 8px;direction:ltr}.no-chart{height:94px;padding-top:24px;color:var(--muted);font-size:12px}
.comparisons{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding-top:12px;border-top:1px solid var(--line)}.comparisons span{display:block;font-size:10px;color:var(--muted)}.comparisons strong{font-size:16px;font-weight:500}.comparisons small{font-size:11px}.note{font-size:11px;min-height:42px;margin:14px 0 0}
.table-card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}.scroll{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:right;padding:16px 20px;border-bottom:1px solid var(--line);white-space:nowrap}thead th{font-size:11px;color:var(--muted);font-weight:400;background:#142b3c}tbody th{font-weight:500}td strong{font-weight:400;font-size:16px}td small{font-size:12px}.cell-detail{display:block;color:var(--muted);font-size:10px;margin-top:3px}tr:last-child>*{border-bottom:0}
.table-note{padding:10px 18px;color:var(--muted);font-size:11px;border-top:1px solid var(--line)}details{margin-top:24px;border:1px solid var(--line);border-radius:16px;background:#0d1e2d;overflow:hidden}summary{cursor:pointer;padding:17px 20px;color:var(--accent)}summary:focus-visible,a:focus-visible{outline:2px solid #90eddb;outline-offset:3px}details table{font-size:11px}details td{padding:12px 16px}a{color:var(--accent)}footer{margin-top:22px;color:var(--muted);font-size:11px;line-height:2}bdi{font-variant-numeric:tabular-nums}
@media(max-width:980px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.card-top h2{font-size:14px}}
@media(max-width:620px){main{padding:22px 14px 40px}h1{font-size:25px}.headline{align-items:flex-start;gap:10px}.mode{font-size:11px;padding:5px 9px;margin-top:16px}.grid{grid-template-columns:1fr}.summary{gap:8px}.summary strong{font-size:19px}.summary span{font-size:10px}.notice{display:block;padding:13px 15px}.notice b{display:block;margin-bottom:4px}.section-title{display:block}.metric{padding:18px}.note{min-height:0}th,td{padding:14px}.brand{font-size:10px}}
</style></head><body><main><header><div class="brand">BITPIN / DAILY DEFI OBSERVATORY</div>
<div class="headline"><div><h1>بازار، زیر ذره‌بین روزانه</h1><p class="muted">داده‌های عمومی DefiLlama برای ارزیابی شواهد تصمیم روزانه</p></div><div class="mode">● حالت آزمایشی</div></div>
<div class="summary"><div><span>منابع قابل بررسی</span><strong><bdi>__GOOD__ / __TOTAL__</bdi></strong></div><div><span>شبکه‌های منتخب</span><strong>__CHAINS__</strong></div><div><span>اثر بر تصمیم واقعی</span><strong>غیرفعال</strong></div></div>
<div class="notice"><b>روز کامل، برداشت محتاطانه</b><span>ردیف امروز UTC از محاسبات حذف می‌شود. داده‌ها در این آزمایش فقط ثبت و مقایسه می‌شوند؛ هیچ پیام مدل یا سفارشی تولید نمی‌شود.</span></div></header>
<div class="section-title"><h2>نمای کلی DeFi</h2><span>گزارش <bdi>__STAMP__</bdi> به وقت تهران</span></div><div class="grid">__CARDS__</div>
<div class="section-title"><h2>فعالیت در شبکه‌ها</h2><span>شبکه‌ها نمایندهٔ قیمت یا تقاضای مستقیم توکن بومی نیستند</span></div>
<section class="table-card"><div class="scroll"><table><thead><tr><th>شبکه</th><th>TVL · دلار</th><th>حجم DEX · میانگین روزانهٔ ۷ روز، دلار</th><th>عرضه · واحد توکن دلاری</th></tr></thead><tbody>__ROWS__</tbody></table></div>
<div class="table-note">درصد زیر TVL و عرضه: تغییر نسبت به هفت روز قبل. درصد زیر حجم: هفتهٔ اخیر در مقایسه با هفتهٔ قبل. تاریخ هر سلول، روز UTC داده است.</div></section>
<details><summary>کیفیت داده، تاریخ مشاهده‌ها و پیوند منابع</summary><div class="scroll"><table><thead><tr><th>شاخص / محدوده</th><th>وضعیت</th><th>آخرین روز UTC</th><th>سن دریافت</th><th>پوشش هفته</th><th>منبع</th></tr></thead><tbody>__AUDIT__</tbody></table></div></details>
<footer>خط تیره یعنی دادهٔ کافی یا معتبر موجود نیست؛ به معنی صفر نیست. تاریخچهٔ منبع ممکن است بازبینی شود و دامنهٔ پروتکل‌های تحت پوشش تغییر کند. روز سپری‌شده، تضمین نهایی‌بودن داده نیست. مبالغ TVL و حجم با اثر قیمت تغییر می‌کنند. این گزارش مزیت معاملاتی یا سودآوری را اثبات نمی‌کند.<br>
داده: <a href="https://api-docs.defillama.com/llms-free.txt" rel="noreferrer">API رایگان DefiLlama</a> · <a href="https://docs.llama.fi/analysts/data-definitions" rel="noreferrer">تعریف شاخص‌ها</a> · قلم Vazirmatn، مجوز SIL OFL</footer></main></body></html>'''
    for key, value in {"FONT": font, "GOOD": good, "TOTAL": len(sources), "CHAINS": len(chains),
                       "STAMP": stamp, "CARDS": "".join(cards), "ROWS": "".join(rows),
                       "AUDIT": "".join(audit)}.items():
        template = template.replace("__" + key + "__", str(value))
    return template


def write_report(snapshot, directory):
    atomic_text(directory, "report.html", render_report(snapshot))
