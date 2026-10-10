"""Self-contained Persian report for public shadow observations."""
import html
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

LABELS = {"ready": "آمادهٔ بررسی", "warming_up": "در حال جمع‌آوری", "missing": "داده موجود نیست",
          "error": "خطای دریافت", "stale": "داده کهنه", "ok": "دریافت موفق"}


def esc(value):
    return html.escape(str(value), quote=True)


def fmt(value, digits=2):
    return "—" if value is None else ("{:,.%df}" % digits).format(value)


def badge(status):
    return '<span class="badge %s">%s</span>' % (esc(status), LABELS.get(status, esc(status)))


def render_report(snapshot):
    stamp = datetime.fromtimestamp(snapshot["generated_at"], timezone(timedelta(hours=3, minutes=30)))
    cards = []
    for symbol, market in snapshot["markets"].items():
        unit = "تومان" if market["quote_unit"] == "IRT" else "USDT"
        rows = []
        for seconds, label in ((300, "۵ دقیقه"), (900, "۱۵ دقیقه"), (3600, "۱ ساعت"),
                               (14400, "۴ ساعت"), (86400, "۲۴ ساعت")):
            row = market["windows"][str(seconds)]
            ratio = row["buy_ratio"]
            rows.append('<tr><td>%s</td><td>%s</td><td class="number">%s</td><td class="number">%s</td>'
                        '<td class="number">%s</td></tr>' %
                        (label, badge(row["quality"]), fmt(row["delta_quote"]),
                         fmt(ratio * 100, 1) + "٪" if ratio is not None else "—", fmt(row["trade_count"], 0)))
        book = market["book"]
        data = book.get("data") or {}
        depth_rows = []
        for pct, band in data.get("bands", {}).items():
            depth_rows.append('<tr><td>%s٪</td><td class="number">%s</td><td class="number">%s</td>'
                              '<td>%s</td></tr>' %
                              (pct, fmt(band["bid_quote"]), fmt(band["ask_quote"]),
                               "پوشش تا مرز بازه" if band["complete"] else "عمق ناقص"))
        error = market["trades"].get("error")
        cards.append('<section class="card"><div class="title"><h2 dir="ltr">%s</h2>%s</div>'
                     '<p class="muted">واحد ارزش: %s · معاملات یکتا: %s · سن دریافت: %s ثانیه</p>'
                     '%s<div class="scroll"><table><thead><tr><th>بازه</th><th>کیفیت</th>'
                     '<th>دلتای خرید و فروش</th><th>سهم خرید تهاجمی</th><th>معامله</th>'
                     '</tr></thead><tbody>%s</tbody></table></div>'
                     '<details><summary>دفتر سفارش و کیفیت پوشش</summary><p>وضعیت دفتر: %s · '
                     'فاصلهٔ خرید و فروش: %s٪ · سن دریافت دفتر: %s ثانیه</p><div class="scroll"><table>'
                     '<thead><tr><th>فاصله از میانه</th><th>ارزش خرید</th><th>ارزش فروش</th><th>پوشش</th>'
                     '</tr></thead><tbody>%s</tbody></table></div>'
                     '<p class="muted">این اعداد فقط عمق مشاهده‌شده‌اند. سفارش باز، معاملهٔ انجام‌شده نیست.'
                     ' شمار گسست‌های ثبت‌شده: %s</p></details></section>' %
                     (esc(symbol), badge(market["trades"]["status"]), unit, market["total_unique_trades"],
                      fmt(market["trades"].get("receipt_age_seconds"), 0),
                      '<p class="error-text" dir="ltr">%s</p>' % esc(error) if error else "",
                      "".join(rows), badge(book["status"]), fmt(data.get("spread_pct")),
                      fmt(book.get("receipt_age_seconds"), 0), "".join(depth_rows),
                      market["trades"].get("gap_count", 0)))
    liquidity = snapshot["liquidity"]
    macro = liquidity.get("data") or {}
    macro_rows = []
    for symbol, data in macro.get("assets", {}).items():
        macro_rows.append('<tr><td dir="ltr">%s</td><td class="number">%s</td><td class="number">%s</td>'
                          '<td class="number">%s</td><td class="number">%s</td></tr>' %
                          (esc(symbol), fmt(data["circulating_units"], 0),
                           fmt(data["change_1d_units"], 0), fmt(data["change_7d_units"], 0),
                           fmt(data["change_30d_units"], 0)))
    return """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>آزمایش جریان پول بیت‌پین</title><style>
:root{color-scheme:dark;--bg:#0b1220;--card:#141f30;--border:#2b394d;--text:#edf3fb;--muted:#aebdd0;--accent:#64dfcb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.9 Vazirmatn,Tahoma,"Segoe UI",sans-serif}
main{max-width:1180px;margin:auto;padding:36px 24px 56px}header{padding:12px 0 24px}h1{font-size:30px;line-height:1.5;margin:14px 0 8px}
h2{font-size:18px;margin:0}p{margin:10px 0}.eyebrow{font-size:13px;color:var(--accent);letter-spacing:.02em}
.muted,footer{color:var(--muted);font-size:13px}.notice{border:1px solid #286a65;background:#123832;padding:16px 20px;border-radius:14px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:22px;margin:18px 0;box-shadow:0 8px 30px #0001}
.title{display:flex;align-items:center;justify-content:space-between;gap:12px}.badge{display:inline-block;border:1px solid #64738a;border-radius:999px;padding:2px 10px;font-size:12px;white-space:nowrap;color:#dae4f0}
.badge.ok,.badge.ready{color:#8ce8d2;border-color:#407f73;background:#173d36}.badge.error,.badge.stale{color:#ffbcc0;border-color:#87565b;background:#3a242d}
.badge.warming_up{color:#ffe0a6;border-color:#78613c;background:#392f23}.scroll{overflow-x:auto}table{width:100%%;border-collapse:collapse;font-size:13px}
th{text-align:right;color:var(--muted);font-weight:400;border-bottom:1px solid var(--border);padding:12px 10px;white-space:nowrap}
td{padding:14px 10px;border-bottom:1px solid #273449;white-space:nowrap}tbody tr:last-child td{border-bottom:0}.number{font-variant-numeric:tabular-nums;direction:ltr;text-align:right}
details{margin-top:16px;border-top:1px solid var(--border);padding-top:14px}summary{cursor:pointer;color:var(--accent);padding:5px}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:5px;border-radius:5px}.error-text{color:#ffbcc0;overflow-wrap:anywhere}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.stat{padding:18px;border:1px solid var(--border);border-radius:14px}.stat strong{font-size:24px;display:block}
footer{margin-top:24px}a{color:var(--accent)}@media(max-width:650px){main{padding:20px 12px}h1{font-size:24px}.card{padding:15px}.grid{grid-template-columns:1fr}.title{flex-wrap:wrap}}
</style></head><body><main><header><div class="eyebrow">BITPIN · FLOW OBSERVATORY</div>
<h1>آزمایش جریان پول</h1><p class="muted">گزارش %s به وقت تهران · نسخهٔ آزمایشی داده‌های عمومی</p>
<div class="notice"><strong>فقط مشاهده و ارزیابی</strong><br>این جمع‌آورنده به حساب معاملاتی و مدل تصمیم‌گیر متصل نیست.
 هیچ سفارش یا تغییر وزنی از این سیگنال‌ها ایجاد نمی‌شود.</div>
<div class="grid"><div class="stat"><span class="muted">بازارهای زیر نظر</span><strong>%s</strong></div>
<div class="stat"><span class="muted">سفارش ایجادشده توسط آزمایش</span><strong>۰</strong></div>
<div class="stat"><span class="muted">فراخوانی مدل در آزمایش</span><strong>۰</strong></div></div></header>
<section class="card"><div class="title"><h2>نقدینگی استیبل‌کوین‌ها</h2>%s</div>
<p class="muted">DefiLlama · تغییر تعداد توکن‌های دلاری در گردش · سن دریافت: %s ثانیه</p>
<div class="scroll"><table><thead><tr><th>دارایی</th><th>عرضهٔ فعلی</th><th>تغییر یک‌روزه</th><th>تغییر هفت‌روزه</th><th>تغییر سی‌روزه</th></tr></thead><tbody>%s</tbody></table></div>
<p class="muted">زمان به‌روزرسانی منبع در پاسخ API اعلام نشده است. عرضهٔ استیبل‌کوین معادل ورود پول به صرافی یا خرید رمزارز نیست.</p>%s</section>
%s<footer>دلتا = ارزش خرید تهاجمی منهای فروش تهاجمی در همان بازار. خط تیره یعنی دادهٔ قابل استفاده نداریم.
بازه‌ها پس از جمع‌آوری پیوسته آماده می‌شوند؛ قطعی یا نبود هم‌پوشانی، زمان جمع‌آوری را از نو شروع می‌کند.
این گزارش بازده یا سودآوری راهبرد را اندازه نمی‌گیرد.</footer></main></body></html>""" % (
        stamp.strftime("%Y-%m-%d · %H:%M:%S"), len(snapshot["markets"]), badge(liquidity["status"]),
        fmt(liquidity.get("receipt_age_seconds"), 0), "".join(macro_rows),
        '<p class="error-text" dir="ltr">%s</p>' % esc(liquidity.get("error")) if liquidity.get("error") else "",
        "".join(cards))


def write_reports(snapshot, directory):
    directory = Path(directory)
    for name, text in (("snapshot.json", json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2)),
                       ("report.html", render_report(snapshot))):
        target = directory / name
        temp = directory / (name + ".tmp")
        if target.is_symlink() or temp.is_symlink():
            raise ValueError("report symlinks are not allowed")
        with open(str(temp), "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(target))
