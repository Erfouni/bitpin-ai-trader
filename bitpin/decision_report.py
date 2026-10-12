"""Read-only, deterministic decision and execution reporting (stdlib, Python 3.7+).

Targets and model votes are intentions. Only a linked order's known positive
executed amount is a fill. Old/unlinked history is deliberately unverified.
"""
import math
import re
from itertools import islice


_SYMBOL = re.compile(r"^[A-Z0-9]{1,15}_[A-Z0-9]{2,8}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_KNOWN = frozenset(("filled", "partial", "open", "closed", "cancelled", "canceled"))
_FINAL = frozenset(("filled", "partial", "closed", "cancelled", "canceled", "unfilled", "not_sent",
                    "not_found", "rejected"))
_STATES = frozenset(("unverified", "no_fill", "filled", "partial", "pending", "preview"))


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and 0 <= n < 1e100 else None


def _symbol(value):
    return value if isinstance(value, str) and _SYMBOL.fullmatch(value) else None


def _identifier(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _weights(value):
    value = _dict(value)
    # The bot's full universe contains zero-weight symbols too. Scan the complete
    # bounded mapping, not the first few entries, or both targets and cash lie.
    if len(value) > 256:
        return {}
    out = {}
    for s, w in value.items():
        n = _number(w)
        if not _symbol(s) or n is None or n > 1:
            return {}
        if n > 0:
            out[s] = n
    return out if sum(out.values()) <= 1.000001 else {}


def _order(value):
    r = _dict(value)
    sym, side = _symbol(r.get("symbol")), r.get("side")
    ident, oid = _identifier(r.get("identifier")), _identifier(r.get("order_id"))
    if not sym or side not in ("buy", "sell") or not (ident or oid):
        return None
    status = r.get("status") if isinstance(r.get("status"), str) else "unknown"
    status = status if status in _KNOWN | _FINAL | frozenset(("unknown", "submitting", "resting")) else "unknown"
    out = {"identifier": ident, "order_id": oid, "side": side, "symbol": sym, "status": status,
           "partial": r.get("partial") is True}
    for k in ("base", "quote", "fee", "requested", "avg_price"):
        out[k] = _number(r.get(k))
    asset = r.get("fee_asset")
    out["fee_asset"] = asset if isinstance(asset, str) and re.fullmatch(r"[A-Z0-9]{1,15}", asset) else None
    return out


def _filled(order):
    return order["status"] in _KNOWN and (order.get("base") or 0) > 0


def _partial(order):
    amount = order.get("quote") if order["side"] == "buy" else order.get("base")
    requested = order.get("requested")
    return (order["partial"] or order["status"] in ("partial", "open")
            or (requested is not None and requested > 0 and amount is not None and amount < requested * 0.99))


def _journal_order(order, journal):
    """Refresh this exact captured market identifier, without any broker/API calls."""
    ident = order.get("identifier")
    e = _dict(_dict(_dict(journal).get("orders")).get(ident)) if ident else {}
    if (not e or e.get("kind", "market") != "market" or e.get("symbol") != order["symbol"]
            or e.get("side") != order["side"]):
        return order
    r = dict(order, status=e.get("status", "unknown"))
    for k in ("base", "quote", "fee", "fee_asset"):
        if "reported_" + k in e:
            r[k] = e["reported_" + k]
    r["order_id"] = e.get("order_id") or order.get("order_id")
    requested = e.get("quote_amount") if order["side"] == "buy" else e.get("base_amount")
    if requested is not None:
        r["requested"] = requested
    # A journal describes lifecycle; compute partial/average from cumulative amounts.
    r["partial"] = False
    result = _order(r)
    if result and result.get("base") and result.get("quote") is not None:
        result["avg_price"] = _number(result["quote"] / result["base"])
    return result or order


def execution_view(decision, records=(), journal=None, mode="live"):
    """Bounded exact-reference projection; no fill proof from prose, targets or timestamps nearby.

    Cumulative updates for one identifier count once. Paper and dry-run receipts
    never satisfy a live projection. ``filled`` means confirmed amounts exist;
    it does not assert that the full rebalance completed or target weights hold.
    """
    d = _dict(decision)
    runtime = mode if mode in ("live", "paper", "dry_run") else "live"
    result = {"schema_version": 1, "state": "unverified", "mode": runtime, "fills": [], "pending": 0,
              "orders": 0, "skipped": 0, "errors": 0, "effective_targets": {}}
    t = _number(d.get("decided_at", d.get("t")))
    origin = d.get("origin")
    if not t or not isinstance(origin, str) or not origin or len(origin) > 100:
        return result
    if not isinstance(records, (list, tuple)):
        return result
    orders, confirmed = {}, {}
    unidentified = 0
    matched = False
    for rec in records[-512:]:
        e = _dict(_dict(rec).get("execution"))
        ref = _dict(e.get("decision_ref"))
        if (type(e.get("schema_version")) is not int or e["schema_version"] != 1
                or e.get("runtime_mode") != runtime or _number(ref.get("decided_at")) != t
                or ref.get("origin") != origin or not isinstance(e.get("orders"), list)):
            continue
        matched = True
        effective = _weights(e.get("effective_targets"))
        if effective:
            result["effective_targets"] = effective
        for k in ("skipped", "errors"):
            result[k] += min(len(e[k]), 100) if isinstance(e.get(k), list) else 0
        for raw in e["orders"][:100]:
            o = _order(raw)
            if not o:
                r = _dict(raw)
                if (_symbol(r.get("symbol")) and r.get("side") in ("buy", "sell")
                        and r.get("status") in ("unknown", "submitting", "open")):
                    # Without an ID we cannot refresh or count a fill, but an
                    # ambiguous submission must not become a known empty result.
                    unidentified = min(100, unidentified + 1)
                continue
            key = ("i", o["identifier"]) if o["identifier"] else ("o", o["order_id"])
            if key not in orders and len(orders) >= 100:
                continue
            prior = orders.get(key)
            if prior and (prior["symbol"], prior["side"]) != (o["symbol"], o["side"]):
                continue
            orders[key] = o
            if _filled(o) and (key not in confirmed or o["base"] >= confirmed[key]["base"]):
                confirmed[key] = o
    if not matched:
        return result
    for key, order in list(orders.items()):
        fresh = _journal_order(order, journal) if runtime == "live" else order
        orders[key] = fresh
        if _filled(fresh) and (key not in confirmed or fresh["base"] >= confirmed[key]["base"]):
            confirmed[key] = fresh
    fills = []
    for key in sorted(confirmed):
        fill = dict(confirmed[key])
        fill["partial"] = _partial(fill)
        fills.append(fill)
    result.update(fills=fills[:100], orders=len(orders) + unidentified,
                  pending=sum(o["status"] not in _FINAL for o in orders.values()) + unidentified)
    result["skipped"], result["errors"] = min(result["skipped"], 100), min(result["errors"], 100)
    if runtime != "live":
        result["state"] = "preview"
    elif result["pending"]:
        result["state"] = "pending"
    elif fills:
        result["state"] = "partial" if any(f["partial"] for f in fills) else "filled"
    else:
        result["state"] = "no_fill"
    return result


def decision_report_fa(decision, execution=None, limit=800):
    """Final structured intent plus execution status; model strings cannot override either."""
    d = _dict(decision)
    e = _dict(execution)
    if d.get("valid") is False:
        text = "تصمیم نامعتبر است؛ هدف آن مجوز ارسال سفارش نیست."
    else:
        effective = _weights(e.get("effective_targets"))
        targets = effective or _weights(d.get("targets"))
        parts = ["%s %s%%" % (s, ("%.1f" % (w * 100)).rstrip("0").rstrip("."))
                 for s, w in sorted(targets.items())]
        cash = max(0.0, 1.0 - sum(effective.values())) if effective else _number(d.get("cash_irt", d.get("cash")))
        if cash is not None and 1e-8 < cash <= 1:
            parts.append("تومان %s%%" % ("%.1f" % (cash * 100)).rstrip("0").rstrip("."))
        text = "ترکیب هدف نهایی پس از کنترل‌ها: %s." % ("، ".join(parts) if parts else "هدف قابل‌نمایشی ثبت نشده")
        if d.get("hold") is True:
            text += " تغییر وزن جدیدی تأیید نشده است."
        blocked = [s for s in islice(_dict(d.get("p_rule")).keys(), 12) if _symbol(s)]
        if blocked:
            text += " افزایش سهم با قانون احتمال رد شد: %s." % "، ".join(sorted(blocked))
        c = _dict(d.get("confirmation"))
        if _weights(c.get("cut")):
            text += " بخشی از خرید پیشنهادی در بررسی دوم تأیید نشد."
        if _weights(c.get("kept")):
            text += " بررسی دوم فقط افزایش وزن پیشنهادی را تأیید کرده است."
        if isinstance(d.get("adjustments"), list) and d["adjustments"]:
            text += " اصلاحات کد در اهداف نهایی اعمال شده‌اند."
    intent = text
    text = ""
    state = e.get("state") if isinstance(e.get("state"), str) and e["state"] in _STATES else "unverified"
    runtime = e.get("mode")
    if runtime in ("paper", "dry_run") or state == "preview":
        text += " اجرای آزمایشی است؛ هیچ نتیجه‌ای در این گزارش تأیید معاملهٔ واقعی نیست."
    elif state in ("filled", "partial", "pending"):
        fills = [_order(f) for f in e.get("fills", [])[:12]] if isinstance(e.get("fills"), list) else []
        fills = [f for f in fills if f and _filled(f)]
        if fills:
            text += " مقدار اجرای واقعی ثبت‌شده است؛ رسیدن کامل به وزن‌های هدف تأیید نشده است."
        else:
            text += " اجرای واقعی با مقدار مثبت تأیید نشده است."
        if state == "partial":
            text += " بخشی از سفارش انجام شده است."
        if state == "pending":
            text += " نتیجهٔ کامل سفارش‌های باز یا نامعلوم هنوز مشخص نیست."
        if fills:
            text += " مقادیر: " + "، ".join(
                "%s %s %.8g" % ("خرید" if f["side"] == "buy" else "فروش", f["symbol"], f["base"])
                for f in fills) + "."
    elif state == "no_fill":
        text += " در اجرای مرتبط، سفارش انجام‌شده با مقدار مثبت ثبت نشده است."
    else:
        text += " اجرای سفارش برای این تصمیم هنوز تأیید نشده است."
    n = limit if type(limit) is int and 100 <= limit <= 3500 else 800
    # Execution truth gets the space first; a long proposal cannot cut its warning.
    text = text[:n]
    if len(intent) + len(text) > n:
        intent = intent[:max(0, n - len(text) - 1)] + "…" if len(text) < n else ""
    return (intent + text)[:n]
