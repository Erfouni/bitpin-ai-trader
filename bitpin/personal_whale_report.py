"""Offline personal-wallet research report, with exact ETH and attribution limits."""
import base64
from datetime import datetime, timezone
import html
from pathlib import Path

from .defi_daily import atomic_text
from .personal_whale import ADDRESS, ATTRIBUTION


def esc(value): return html.escape(str(value), quote=True)


def eth(value):
    if value is None: return 'Unknown'
    value_abs = abs(value)
    return ('-' if value < 0 else '') + format(value_abs // 10**18, ',') + '.%018d' % (value_abs % 10**18)


def stamp(value):
    return 'Not collected yet' if value is None else datetime.fromtimestamp(value, timezone.utc).strftime('%d %b %Y · %H:%M UTC')


def render_report(snapshot):
    good = snapshot['status'] == 'ok'
    balance = snapshot.get('balance_wei') if good else None
    approximate = 'Unknown' if balance is None else '≈' + format(balance // 10**18, ',') + '.%06d' % (balance % 10**18 // 10**12)
    deltas = []
    for name, title in (('24h', '24-hour change'), ('7d', 'Seven-day change')):
        value = snapshot.get('change_' + name + '_wei') if good else None
        elapsed = snapshot.get('reference_' + name + '_elapsed_seconds') if good else None
        block_elapsed = snapshot.get('reference_' + name + '_block_elapsed_seconds') if good else None
        reference = 'Awaiting observed history' if elapsed is None else 'Receipt interval: %.2f hours' % (elapsed / 3600)
        block_reference = 'Block interval: unknown' if block_elapsed is None else 'Block interval: %.2f hours' % (block_elapsed / 3600)
        deltas.append('<div class="delta"><span>%s</span><strong>%s</strong><small>%s<br>%s</small></div>' %
                      (title, ('+' if value is not None and value > 0 else '') + eth(value), esc(reference), esc(block_reference)))
    sources = []
    for key, title in (('publicnode', 'PublicNode'), ('flashbots', 'Flashbots')):
        row = snapshot.get('sources', {}).get(key, {})
        sources.append('<div class="source"><span class="dot %s"></span><div><b>%s</b><small>%s · finalized height %s</small></div></div>' %
                       ('green' if row.get('status') == 'ok' and good else '', title, esc(row.get('status', 'missing')),
                        esc(row.get('finalized_height', 'unknown'))))
    anchor = snapshot.get('anchor') if good else None
    font_path = Path(__file__).parent / 'static' / 'fonts' / 'Vazirmatn-Regular.woff2'
    font = '@font-face{font-family:Vazirmatn;src:url(data:font/woff2;base64,%s);font-display:swap}' % \
        base64.b64encode(font_path.read_bytes()).decode('ascii') if font_path.is_file() else ''
    css = """*{box-sizing:border-box}body{margin:0;background:#09111e;color:#e8eef7;font:14px/1.7 Vazirmatn,system-ui,sans-serif}
a{color:#b7adff;text-decoration:none}a:hover{text-decoration:underline}a:focus-visible{outline:2px solid #c0b0ff;outline-offset:4px}
main{max-width:1120px;margin:auto;padding:48px 28px}header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}
.eyebrow{color:#b7adff;letter-spacing:.15em;text-transform:uppercase;font-size:11px;font-weight:700}h1{font-size:clamp(30px,5vw,48px);line-height:1.15;letter-spacing:-.035em;margin:12px 0}h2{font-size:22px;margin:12px 0}
p{color:#a7b5ca}.intro{max-width:740px}.pill{display:inline-block;padding:7px 14px;border:1px solid #4a4167;border-radius:999px;color:#cebcff;background:#261f3b;font-size:12px;white-space:nowrap}
.grid{display:grid;grid-template-columns:1.5fr 1fr;gap:20px;margin-top:28px}.card{border:1px solid #26334a;border-radius:22px;background:linear-gradient(135deg,#152238,#0f192b);padding:28px;min-width:0}
.balance{font-size:clamp(27px,4vw,43px);font-weight:700;letter-spacing:-.04em;margin:20px 0 0}.balance small{font-size:15px;color:#94a5bd}.exact{font-size:12px;color:#b3bfd2;overflow-wrap:anywhere}.deltas{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:24px 0}.delta{background:#0b1424;border-radius:14px;padding:16px;min-width:0}.delta span,.delta small{display:block;color:#93a4bd;font-size:12px}.delta strong{display:block;font-size:14px;overflow-wrap:anywhere;padding:8px 0}.address{display:block;padding:12px 0 0;border-top:1px solid #29354d;overflow-wrap:anywhere;font-size:12px}
.source{display:flex;gap:12px;align-items:center;margin:20px 0}.source small{display:block;color:#94a5bd;font-size:12px}.dot{height:8px;width:8px;flex-shrink:0;border-radius:50%;background:#e6b968}.green{background:#6de0ad}
.metadata{border-top:1px solid #29354d;padding-top:16px;font-size:12px;color:#a8b8cf;overflow-wrap:anywhere}.notice{border:1px solid #4d3d24;background:#211c15;border-radius:18px;padding:22px;margin-top:20px}.notice h2{color:#efd4a0;font-size:16px;margin:0 0 8px}.notice p{margin:5px 0;color:#b7b3ab}.scope{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}.scope h2{font-size:17px}.scope p{font-size:13px}footer{color:#8191aa;font-size:12px;margin-top:26px}
@media(max-width:700px){main{padding:28px 18px}header{display:block}.pill{margin-top:8px}.grid,.scope{grid-template-columns:1fr}.card{padding:22px}.deltas{grid-template-columns:1fr}.balance{font-size:32px}h1{font-size:34px}}
"""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' \
        '<title>Personal whale · Shadow observation</title><style>%s%s</style></head><body><main>' \
        '<header><div><div class="eyebrow">Bitpin research · Ethereum mainnet</div><h1>Personal whale observations</h1></div>' \
        '<span class="pill">SHADOW ONLY · %s</span></header><p class="intro">A small public-attribution experiment beyond exchange custody wallets. '
        'One labelled personal Safe, observed at a finalized block agreed by two RPC providers.</p>' \
        '<div class="grid"><article class="card"><div class="eyebrow">01 · Publicly attributed to Vitalik Buterin</div><h2>Vb 3 · Ethereum Safe</h2>' \
        '<div class="balance">%s <small>ETH</small></div><div class="exact">Exact native balance: %s ETH<br>Headline truncated to six decimals.</div>' \
        '<div class="deltas">%s</div><a class="address" href="%s" target="_blank" rel="noreferrer">%s ↗</a></article>' \
        '<aside class="card"><div class="eyebrow">Observation integrity</div><h2>Source agreement</h2>%s<div class="metadata">' \
        'Receipt: %s<br>Finalized block: %s<br>Block time: %s<br>Requests: %s · Orders: 0 · Model calls: 0</div></aside></div>' \
        '<section class="notice"><h2>Current ownership unverified</h2><p>The address uses Etherscan’s public Vb 3 label and published individual-holder research. '
        'This is attribution, not a current signature proving ownership or a complete inventory of this person’s wallets.</p></section>' \
        '<div class="scope"><section class="card"><h2>What this measures</h2><p>Finalized native ETH balance and observed changes near 24 hours and seven days. '
        'Unknown history stays unknown. Failed, disputed or stale observations hide metrics.</p></section>' \
        '<section class="card"><h2>How to interpret it</h2><p>Transfers, fees, donations and funding can change a balance. Tokens, staking and DeFi positions are excluded. '
        'These observations do not establish purchases, sales, gross money flows or profitable predictions.</p></section></div>' \
        '<footer>One profile is not a representative independent-whale cohort. No live decision effect. Attribution checked 11 Oct 2026. '
        '<a href="%s" target="_blank" rel="noreferrer">Published profile research ↗</a><br>This static report describes its receipt time; regenerate before treating an old copy as current.</footer>' \
        '</main></body></html>' % (font, css, esc(snapshot['status'].upper()), approximate, eth(balance), ''.join(deltas),
        ATTRIBUTION['address_source'], ADDRESS, ''.join(sources), esc(stamp(snapshot.get('received_at'))),
        esc(anchor['number'] if anchor else 'Unknown'), esc(stamp(anchor['timestamp'] if anchor else None)),
        esc(snapshot.get('request_count', 'Unknown')), ATTRIBUTION['profile_source']))


def write_report(snapshot, directory):
    atomic_text(directory, 'report.html', render_report(snapshot))
