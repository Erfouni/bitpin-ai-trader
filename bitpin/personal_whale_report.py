"""Offline personal-wallet research report, with exact ETH and attribution limits."""
import base64
from datetime import datetime, timezone
import html
from pathlib import Path

from .defi_daily import atomic_text
from .personal_wallets import WALLETS
from .personal_whale import ADDRESS


def esc(value): return html.escape(str(value), quote=True)


def eth(value):
    if value is None: return 'Unknown'
    value_abs = abs(value)
    return ('-' if value < 0 else '') + format(value_abs // 10**18, ',') + '.%018d' % (value_abs % 10**18)


def stamp(value):
    return 'Not collected yet' if value is None else datetime.fromtimestamp(value, timezone.utc).strftime('%d %b %Y · %H:%M UTC')


def wallet_card(wallet, row, good, index):
    usable = good and row.get('status') == 'ok'
    balance = row.get('balance_wei') if usable else None
    approximate = 'Unknown' if balance is None else '≈' + format(balance // 10**18, ',') + '.%06d' % (balance % 10**18 // 10**12)
    deltas = []
    for name, title in (('24h', '24-hour change'), ('7d', 'Seven-day change')):
        value = row.get('change_' + name + '_wei') if usable else None
        elapsed = row.get('reference_' + name + '_elapsed_seconds') if usable else None
        block_elapsed = row.get('reference_' + name + '_block_elapsed_seconds') if usable else None
        reference = 'Awaiting observed history' if elapsed is None else 'Receipt interval: %.2f hours' % (elapsed / 3600)
        block_reference = 'Block interval: unknown' if block_elapsed is None else 'Block interval: %.2f hours' % (block_elapsed / 3600)
        previous = row.get('reference_' + name + '_balance_wei') if usable else None
        previous_text = '' if previous is None else '<br>Reference balance: %s ETH' % eth(previous)
        deltas.append('<div class="delta"><span>%s</span><strong>%s</strong><small>%s<br>%s%s</small></div>' %
                      (title, ('+' if value is not None and value > 0 else '') + eth(value), esc(reference),
                       esc(block_reference), previous_text))
    attribution = wallet['attribution']
    confidence = {'public_attribution': 'Public attribution', 'strong_historical': 'Strong historical association',
                  'medium_public': 'Medium public attribution'}[attribution['association_strength']]
    initials = ''.join(word[0] for word in wallet['person'].split())
    return ('<article class="card wallet" aria-labelledby="wallet-%d"><div class="wallet-heading">'
            '<span class="avatar" aria-hidden="true">%s</span><div><div class="eyebrow">Profile %02d · %s</div>'
            '<h2 id="wallet-%d">%s</h2></div></div><div class="wallet-label">%s</div>'
            '<div class="balance">%s <small>ETH</small></div><div class="exact">Exact native balance: %s ETH</div>'
            '<div class="rounding">Headline truncated to six decimals</div><div class="deltas">%s</div>'
            '<div class="attribution"><span class="confidence">%s</span><p>%s</p><p class="limits">%s</p>'
            '<div class="links"><a href="%s" target="_blank" rel="noreferrer">Attribution source ↗</a>'
            '<a href="%s" target="_blank" rel="noreferrer">Profile reference ↗</a></div></div>'
            '<a class="address" href="https://etherscan.io/address/%s" target="_blank" rel="noreferrer">%s ↗</a></article>') % (
                index, esc(initials), index, esc(wallet['kind']), index, esc(wallet['person']), esc(wallet['label']),
                approximate, eth(balance), ''.join(deltas), esc(confidence), esc(attribution['association']),
                esc(attribution['limitations']), esc(attribution['address_source']), esc(attribution['profile_source']),
                wallet['address'], wallet['address'])


def render_report(snapshot):
    good = snapshot['status'] == 'ok'
    observed = {row['address']: row for row in snapshot.get('wallets', []) if isinstance(row, dict) and 'address' in row}
    cards = []
    measured = 0
    for index, wallet in enumerate(WALLETS, 1):
        # Older snapshots carry only Vitalik's scalar; additional history is unknown.
        row = observed.get(wallet['address'], snapshot if wallet['address'] == ADDRESS and 'wallets' not in snapshot else {})
        measured += int(good and row.get('status') == 'ok' and row.get('balance_wei') is not None)
        cards.append(wallet_card(wallet, row, good, index))
    sources = []
    secondary = ('flashbots', 'Flashbots') if 'flashbots' in snapshot.get('sources', {}) \
        and 'drpc' not in snapshot.get('sources', {}) else ('drpc', 'dRPC')
    for key, title in (('publicnode', 'PublicNode'), secondary):
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
main{max-width:1380px;margin:auto;padding:48px 28px}header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}
.eyebrow{color:#b7adff;letter-spacing:.12em;text-transform:uppercase;font-size:10px;font-weight:700}h1{font-size:clamp(30px,5vw,46px);line-height:1.15;letter-spacing:-.035em;margin:12px 0}h2{font-size:20px;line-height:1.4;margin:3px 0}
p{color:#a7b5ca}.intro{max-width:800px;margin:12px 0 26px}.pill{display:inline-block;padding:7px 14px;border:1px solid #4a4167;border-radius:999px;color:#cebcff;background:#261f3b;font-size:12px;white-space:nowrap}
.card{border:1px solid #26334a;border-radius:22px;background:linear-gradient(135deg,#152238,#0f192b);padding:24px;min-width:0}
.integrity{display:grid;grid-template-columns:1fr 1.05fr 1.4fr;gap:24px;align-items:center}.integrity h2{font-size:18px}.summary{font-size:12px;color:#94a5bd}.summary b{color:#e5eef9}.providers{border-left:1px solid #29354d;padding-left:24px}.source{display:flex;gap:12px;align-items:center;margin:10px 0}.source small{display:block;color:#94a5bd;font-size:11px}.dot{height:8px;width:8px;flex-shrink:0;border-radius:50%;background:#e6b968}.green{background:#6de0ad}
.metadata{font-size:11px;color:#a8b8cf;overflow-wrap:anywhere}.metadata b{color:#d9e4f4;font-weight:500}.anchor-hash{display:block;font-size:10px;margin-top:6px;color:#8294ae}
.wallets{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin-top:24px}.wallet{display:flex;flex-direction:column}.wallet-heading{display:flex;align-items:center;gap:12px}.avatar{display:grid;place-items:center;width:42px;height:42px;border-radius:14px;border:1px solid #514471;background:#292040;color:#d6c5ff;font-size:12px;flex-shrink:0}.wallet-label{color:#9cadc5;font-size:12px;margin-top:14px}.balance{font-size:clamp(25px,2.5vw,34px);font-weight:700;letter-spacing:-.04em;margin:8px 0 0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.balance small{font-size:13px;color:#94a5bd}.exact{font-size:11px;color:#bcc8da;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.rounding{font-size:10px;color:#8191aa;margin-top:3px}
.deltas{display:grid;gap:10px;margin:20px 0}.delta{background:#0b1424;border:1px solid #202e43;border-radius:14px;padding:13px 15px;min-width:0}.delta span,.delta small{display:block;color:#9aabc3;font-size:11px}.delta strong{display:block;font-size:13px;overflow-wrap:anywhere;padding:5px 0;color:#e5ecf8;font-variant-numeric:tabular-nums}.delta small{font-size:10px;overflow-wrap:anywhere}
.attribution{border-top:1px solid #29354d;padding-top:16px}.confidence{font-size:10px;color:#c6b4ee;background:#26213b;border-radius:6px;padding:4px 7px;display:inline-block}.attribution p{font-size:11px;line-height:1.7;margin:12px 0}.attribution .limits{color:#d9c299}.links{display:flex;flex-wrap:wrap;gap:5px 15px;font-size:10px}.address{display:block;padding:15px 0 0;margin-top:auto;overflow-wrap:anywhere;font:10px/1.6 ui-monospace,Consolas,monospace;letter-spacing:-.01em}
.notice{border:1px solid #4d3d24;background:#211c15;border-radius:18px;padding:20px 24px;margin-top:20px}.notice h2{color:#efd4a0;font-size:16px;margin:0 0 6px}.notice p{margin:5px 0;color:#b7b3ab;font-size:12px}.scope{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:20px}.scope h2{font-size:17px}.scope p{font-size:12px;margin-bottom:0}footer{color:#8191aa;font-size:11px;margin-top:26px}
.exact,.delta span,.delta small,.attribution p,.links,.source small,.metadata,footer{font-size:12px}.links a{display:inline-flex;align-items:center;min-height:32px}.address{font-size:11px;min-height:40px}.confidence,.rounding{font-size:11px}
@media(max-width:1080px){.wallets{grid-template-columns:repeat(2,minmax(0,1fr))}.wallet:last-child{grid-column:1/-1}.wallet:last-child .deltas{grid-template-columns:1fr 1fr}.integrity{grid-template-columns:1fr 1fr}.metadata{grid-column:1/-1;border-top:1px solid #29354d;padding-top:14px}}
@media(max-width:700px){main{padding:28px 18px}header{display:block}.pill{margin-top:8px}.wallets,.scope,.integrity{grid-template-columns:1fr}.card{padding:22px}.wallet:last-child .deltas{grid-template-columns:1fr}.balance{font-size:32px}h1{font-size:34px}.providers{border-left:0;border-top:1px solid #29354d;padding:10px 0 0}.integrity{gap:12px}.metadata{grid-column:auto}.notice{padding:20px}}
"""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Personal wallets · Shadow observation</title><style>%s%s</style></head><body><main>'
        '<header><div><div class="eyebrow">Bitpin research · Ethereum mainnet</div><h1>Personal wallet observations</h1></div>'
        '<span class="pill">SHADOW ONLY · %s</span></header><p class="intro">Three reviewed public associations, observed at one '
        'finalized Ethereum block agreed by two RPC providers. Native ETH coverage, with each attribution’s limits visible.</p>'
        '<section class="card integrity" aria-label="Observation integrity"><div><div class="eyebrow">Observation integrity</div>'
        '<h2>Source agreement</h2><div class="summary"><b>%d of 3</b> native balances available<br>Requests: %s · Orders: 0 · Model calls: 0</div></div>'
        '<div class="providers">%s</div><div class="metadata"><b>Receipt:</b> %s<br><b>Common finalized block:</b> %s<br>'
        '<b>Block time:</b> %s<span class="anchor-hash">%s</span></div></section>'
        '<section class="wallets" aria-label="Reviewed wallet observations">%s</section>'
        '<section class="notice"><h2>Current ownership unverified</h2><p>Public labels and historical associations do not prove '
        'current control, independent beneficial ownership or that all funds are personal. These profiles are a small, selective sample; '
        'they are not a representative independent-whale cohort.</p></section>'
        '<div class="scope"><section class="card"><h2>What this measures</h2><p>Finalized native ETH balances and individual observed '
        'changes near 24 hours and seven days. Unknown history stays unknown. A failure or disagreement for any wallet, a chain warning, '
        'or a stale receipt hides every usable metric.</p></section><section class="card"><h2>How to interpret it</h2>'
        '<p>Transfers, fees, donations and funding can change balances. Tokens, staking and DeFi positions are excluded. These observations '
        'do not establish purchases, sales, gross flows or profitable predictions.</p></section></div>'
        '<footer>No live decision effect. Attribution reviewed 11 Oct 2026. This static report describes its receipt time; '
        'regenerate before treating an old copy as current.</footer></main></body></html>') % (
            font, css, esc(snapshot['status'].upper()), measured, esc(snapshot.get('request_count', 'Unknown')),
            ''.join(sources), esc(stamp(snapshot.get('received_at'))), esc(anchor['number'] if anchor else 'Unknown'),
            esc(stamp(anchor['timestamp'] if anchor else None)), esc(anchor['hash'] if anchor else 'No usable anchor'), ''.join(cards))


def write_report(snapshot, directory):
    atomic_text(directory, 'report.html', render_report(snapshot))
