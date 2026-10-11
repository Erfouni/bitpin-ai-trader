"""Offline public-wallet research report. No scripts, remote fonts, or live controls."""
import base64
from datetime import datetime, timezone
import html
from pathlib import Path

from .defi_daily import atomic_text
from .whale_public import PROVENANCE

KINDS = {"known_set_internal": "Within published set", "to_unlisted": "To unlisted addresses",
         "from_unlisted": "From unlisted addresses", "ambiguous": "Ambiguous inputs / scripts"}


def esc(value):
    return html.escape(str(value), quote=True)


def btc(value):
    if value is None:
        return "Unknown"
    absolute = abs(value)
    return ("-" if value < 0 else "") + format(absolute // 100000000, ",") + ".%08d" % (absolute % 100000000)


def timestamp(value):
    return "Not collected yet" if value is None else datetime.fromtimestamp(value, timezone.utc).strftime("%d %b %Y · %H:%M UTC")


def change(value):
    return ("+" if value is not None and value > 0 else "") + btc(value)


def render_report(snapshot):
    status = snapshot["status"]
    good = status == "ok"
    sources = snapshot.get("sources", {})
    checks = sum(row.get("status") == "ok" for row in sources.values())
    txs = snapshot.get("transactions", [])
    large = sum(row.get("large_movement", False) for row in txs)
    cards = []
    for index, wallet in enumerate(snapshot["wallets"]):
        elapsed = wallet.get("reference_24h_elapsed_seconds")
        reference = "Awaiting a 24-hour reference" if elapsed is None else "Compared over %.2f hours" % (elapsed / 3600)
        cards.append('<article class="wallet"><div class="wallet-top"><span class="index">%02d</span>'
                     '<span class="pill">BTC · Published custody address</span></div><h2>%s</h2>'
                     '<div class="balance">%s <small>BTC</small></div><p class="caption">Observed chain balance · ≥1 confirmation</p>'
                     '<div class="deltas"><div><span>24-hour change</span><strong>%s</strong></div>'
                     '<div><span>7-day change</span><strong>%s</strong></div></div>'
                     '<p class="reference">%s</p><a class="address" href="https://mempool.space/address/%s" '
                     'rel="noreferrer" target="_blank" aria-label="Open address in mempool explorer">%s ↗</a></article>' %
                     (index + 1, esc(wallet["label"]), btc(wallet.get("balance_sats")),
                      change(wallet.get("change_24h_sats")), change(wallet.get("change_7d_sats")), esc(reference),
                      esc(wallet["address"]), esc(wallet["address"])))
    rows = []
    for row in txs[:12]:
        kind = row["kind"]
        rows.append('<tr><td><span class="direction %s">%s</span>%s</td><td class="numeric">%s <small>BTC</small></td>'
                    '<td>%s</td><td class="numeric">%s</td><td><a href="https://mempool.space/tx/%s" '
                    'target="_blank" rel="noreferrer">%s… ↗</a></td></tr>' %
                    ("internal" if kind == "known_set_internal" else "neutral", esc(KINDS.get(kind, "Unknown")),
                     '<span class="large">≥500 BTC</span>' if row.get("large_movement") else "",
                     btc(row.get("amount_sats")), esc(timestamp(row["block_time"])), esc(row["confirmations"]),
                     esc(row["txid"]), esc(row["txid"][:12])))
    empty = ('<div class="empty"><span>◌</span><h3>%s</h3><p>%s</p></div>' %
             ("No eligible transactions in the current sample" if good else "Waiting for a trustworthy observation",
              "Only transactions with at least six confirmations are shown." if good else
              "Both explorers must agree. Failed or stale observations remain unknown."))
    table = ('<div class="table-scroll" tabindex="0" aria-label="Transfer sample table"><table><thead><tr>'
             '<th>Address-set movement</th><th>Observed amount</th><th>Block timestamp</th><th>Confirmations</th>'
             '<th>Transaction</th></tr></thead><tbody>%s</tbody></table></div>') % "".join(rows) if rows else empty
    provider_cards = []
    for provider, title in (("blockstream", "Blockstream Esplora"), ("mempool", "mempool.space")):
        source = sources.get(provider, {})
        source_status = source.get("status", "missing")
        provider_cards.append('<div class="provider"><span class="dot %s"></span><div><strong>%s</strong>'
                              '<span>%s · tip %s</span></div></div>' %
                              ("green" if source_status == "ok" else "amber", title, esc(source_status),
                               esc(source.get("tip_height", "unknown"))))
    font_file = Path(__file__).parent / "static" / "fonts" / "Vazirmatn-Regular.woff2"
    font = ""
    if font_file.is_file():
        font = "@font-face{font-family:Vazirmatn;src:url(data:font/woff2;base64," + base64.b64encode(
            font_file.read_bytes()).decode("ascii") + ") format('woff2');font-display:swap}"
    css = """*{box-sizing:border-box}body{margin:0;background:#09111e;color:#e6edf6;font-family:Vazirmatn,system-ui,sans-serif;font-size:14px;line-height:1.65}
a{color:#90b8fb;text-decoration:none}a:hover{text-decoration:underline}a:focus-visible,.table-scroll:focus-visible{outline:2px solid #f6b64a;outline-offset:4px}
.shell{max-width:1240px;margin:auto;padding:32px 36px 45px}.brand{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-bottom:25px;border-bottom:1px solid #263148}
.brand-name{display:flex;align-items:center;gap:12px;font-size:16px;font-weight:600}.logo{display:grid;place-items:center;width:38px;height:38px;background:#f0ab3e;color:#161c26;font-size:25px;border-radius:12px}
.top-label,.eyebrow{color:#96a7c0;font-size:11px;letter-spacing:.12em;text-transform:uppercase}.pill{font-size:10px;border:1px solid #344158;color:#b4c6de;border-radius:20px;padding:4px 9px;white-space:nowrap}
.hero{display:flex;justify-content:space-between;align-items:flex-end;gap:28px;padding:34px 0 25px}.eyebrow{color:#eeb656}.hero h1{font-size:clamp(27px,4vw,42px);font-weight:600;letter-spacing:-1.5px;line-height:1.23;margin:9px 0 14px}
.hero p{color:#aebcd0;max-width:675px;margin:0;font-size:14px}.status{flex-shrink:0;padding:15px 20px;border:1px solid #354563;border-radius:16px;background:#111e31;font-size:13px}.status strong{display:block;font-size:16px;color:#f2c16e}.status.ok strong{color:#9adcc5}.caption{font-size:11px;color:#a5b2c7;margin:7px 0}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;margin:5px 0 27px;background:#263148;border:1px solid #263148;border-radius:14px;overflow:hidden}.summary>div{padding:18px 22px;background:#111c2c}.summary span{display:block;font-size:11px;color:#aebcd0}.summary strong{font-size:24px;font-weight:500}.summary small{font-size:12px;font-weight:400;color:#aebcd0}
.notice{display:flex;gap:14px;border:1px solid #4d402b;background:#211e1b;border-radius:14px;padding:17px 20px;margin-bottom:26px}.notice .symbol{font-size:19px;color:#f5bc61}.notice strong{font-size:13px;color:#f0c585}.notice p{color:#c8bba7;font-size:12px;margin:3px 0 0}.wallet-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.wallet{border:1px solid #2a3750;border-radius:17px;padding:20px;background:linear-gradient(145deg,#15233a,#111b2b);min-width:0}.wallet-top{display:flex;align-items:center;justify-content:space-between;gap:8px}.index{color:#f1b857;font-size:12px}.wallet h2{font-weight:500;font-size:15px;margin:20px 0 10px;color:#bccbe0}
.balance{font-size:clamp(18px,2.2vw,27px);font-variant-numeric:tabular-nums;letter-spacing:-.65px;white-space:nowrap}.balance small{font-size:12px;color:#f3bc63;letter-spacing:0}.deltas{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding-top:17px;margin-top:20px;border-top:1px solid #2b3950}.deltas span{font-size:11px;color:#a9b8ce;display:block}.deltas strong{font-size:13px;font-weight:500;display:block;white-space:nowrap;letter-spacing:-.2px}
.reference{font-size:10px;color:#a5b2c7;margin:14px 0}.address{display:block;overflow-wrap:anywhere;font-size:10px;padding-top:13px;border-top:1px solid #2b3950}.section{margin-top:29px;border:1px solid #29374c;border-radius:17px;background:#101b2b;overflow:hidden}.section-heading{padding:20px 23px;border-bottom:1px solid #29374c;display:flex;gap:15px;justify-content:space-between;align-items:center}.section-heading h2{font-size:17px;font-weight:500;margin:0}.section-heading p{margin:4px 0 0;color:#a8b6ca;font-size:11px}
.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;white-space:nowrap;text-align:left}th{font-size:10px;color:#acbad0;font-weight:500;padding:13px 21px;background:#132035}td{padding:14px 21px;border-bottom:1px solid #263249;font-size:11px}tr:last-child td{border:0}.numeric{font-variant-numeric:tabular-nums}.direction{display:inline-block;color:#b5caff;font-size:10px;border:1px solid #384d74;background:#1d2a42;padding:4px 9px;border-radius:7px}.direction.internal{color:#d4c4f8;border-color:#514566;background:#2a243a}.large{font-size:9px;color:#efbd69;margin-left:8px}.empty{text-align:center;padding:40px 25px}.empty>span{font-size:30px;color:#d8ad69}.empty h3{font-weight:500;font-size:15px;margin:10px 0 6px}.empty p{font-size:12px;color:#a8b6ca;margin:0}
.footnote{padding:13px 23px;color:#a8b6ca;font-size:11px;border-top:1px solid #263249}.bottom-grid{display:grid;grid-template-columns:1fr 1.5fr;gap:17px;margin-top:25px}.panel{background:#101b2b;border:1px solid #29374c;border-radius:15px;padding:20px 23px}.panel h2{font-size:14px;font-weight:500;margin:0 0 15px}.provider{display:flex;align-items:center;gap:11px;margin-top:15px}.provider strong{font-size:12px;font-weight:500;display:block}.provider span:not(.dot){display:block;color:#9eafc7;font-size:11px}.dot{height:8px;width:8px;border-radius:50%;display:block}.green{background:#86d9b8}.amber{background:#eab669}.panel p{font-size:12px;color:#abbad0;margin:8px 0}.panel .fine{font-size:11px;color:#9eafc7}footer{display:flex;justify-content:space-between;gap:15px;color:#99abc3;font-size:10px;border-top:1px solid #263148;padding-top:20px;margin-top:29px}
@media(max-width:1000px){.shell{padding:25px 22px}.wallet-grid{grid-template-columns:1fr}.balance{font-size:29px}.wallet h2{margin-top:16px}.deltas strong{font-size:15px}.wallet{padding:22px}.bottom-grid{grid-template-columns:1fr 1fr}}
@media(max-width:640px){.shell{padding:20px 15px 30px}.top-label{display:none}.brand{padding-bottom:18px}.hero{display:block;padding:28px 0 20px}.hero h1{letter-spacing:-.7px}.hero p{font-size:12px}.status{margin-top:20px;padding:12px 16px}.summary{grid-template-columns:1fr 1fr}.summary>div{padding:15px 16px}.notice{padding:15px}.wallet-grid{gap:13px}.wallet{padding:18px}.balance{font-size:26px}.bottom-grid{grid-template-columns:1fr}.section-heading{display:block;padding:17px}.section-heading .pill{display:inline-block;margin-top:10px}.footnote{padding:13px 17px}footer{display:block}footer span{display:block;margin-bottom:7px}}
"""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Bitcoin Wallet Observatory · Shadow research</title><style>%s%s</style></head><body><main class="shell">'
            '<header class="brand"><div class="brand-name"><span class="logo" aria-hidden="true">₿</span>Bitpin Research</div>'
            '<span class="top-label">PUBLIC DATA / WALLET OBSERVATORY</span><span class="pill">Shadow experiment</span></header>'
            '<section class="hero"><div><div class="eyebrow">BITCOIN · CUSTODY-ADDRESS SAMPLE</div><h1>Follow the movement.<br>Keep the motive open.</h1>'
            '<p>Public on-chain observations for daily research. Three historically published addresses, cross-checked with two independent explorers.</p>'
            '</div><div class="status %s"><span class="caption">Current observation</span><strong>%s</strong><span class="caption">%s</span></div></section>'
            '<section class="summary" aria-label="Experiment summary"><div><span>Published addresses</span><strong>3 <small>BTC sample</small></strong></div>'
            '<div><span>Explorer checks</span><strong>%d / 2</strong></div><div><span>Large sampled movements</span><strong>%s <small>≥500 BTC</small></strong></div>'
            '<div><span>Trading impact</span><strong>0 <small>orders · 0 model calls</small></strong></div></section>'
            '<aside class="notice"><span class="symbol" aria-hidden="true">ⓘ</span><div><strong>Attribution has limits.</strong>'
            '<p>Bitfinex published this address list in 2022. Current control has not been reverified. These are custody addresses, not identified private-investor whales. '
            'Unlisted counterparties may belong to the same custodian.</p></div></aside>'
            '<section class="wallet-grid" aria-label="Wallet balances">%s</section>'
            '<section class="section"><div class="section-heading"><div><h2>Confirmed transfer sample</h2><p>Latest 12 of the current bounded sample · block timestamps in UTC</p>'
            '</div><span class="pill">≥6 confirmations</span></div>%s<div class="footnote">Two pages per address per explorer, at most 50 transactions per address. '
            'Amounts describe movements relative to the published address set. This is not complete daily flow, buying pressure, or selling pressure. '
            'Internal moves and transaction fees are kept separate.</div></section>'
            '<div class="bottom-grid"><section class="panel"><h2>Public source health</h2>%s<p class="fine">Independent indexing helps detect disagreement. '
            'Six confirmations reduce reorganization exposure; they do not guarantee finality.</p></section>'
            '<section class="panel"><h2>A daily experiment, with evidence first</h2><p>24-hour and seven-day balance changes need a real observation near the reference time. '
            'Unknown values stay unknown; history will build as collection continues.</p><p>Balance changes include fees and transactions with at least one confirmation. '
            'Transfers alone do not reveal purchases, sales, accumulation, or liquidation.</p><p class="fine">Historical attribution: '
            '<a href="%s" target="_blank" rel="noreferrer">Pinned Bitfinex wallet publication ↗</a> · 12 Dec 2022. '
            'No complete reserves estimate or calibrated predictive edge is claimed.</p></section></div>'
            '<footer><span>No effect on trades · No decision-engine connection</span><span>Report generated %s</span></footer>'
            '</main></body></html>') % (font, css, "ok" if good else "", "Cross-check agreed" if good else esc(status.title()),
                                      esc(timestamp(snapshot.get("received_at"))), checks, large if good else "Unknown",
                                      "".join(cards), table, "".join(provider_cards), esc(PROVENANCE["url"]),
                                      esc(timestamp(snapshot["generated_at"])))


def write_report(snapshot, directory):
    atomic_text(directory, "report.html", render_report(snapshot))
