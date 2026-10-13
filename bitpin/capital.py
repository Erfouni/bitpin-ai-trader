"""Pure capital protection policy; no broker, persistence, or order side effects.

Asset floors are quantities in each asset's own units. Profit reserve is an IRT
partition of existing sleeve cash, rather than extra money or an account deposit.
All calculations preserve finite Decimal amounts without using float arithmetic.
"""
import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, MAX_EMAX, MIN_EMIN, localcontext


ZERO = Decimal(0)
_ASSET_RE = re.compile(r"^[A-Z0-9]+$")


def _asset_name(value):
    if not isinstance(value, str):
        raise ValueError("asset must be a nonempty asset name")
    name = value.strip().upper()
    if not _ASSET_RE.fullmatch(name):
        raise ValueError("invalid asset name: %r" % value)
    return name


def _amount(value, label):
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError("%s must be a finite nonnegative number" % label)
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("%s must be a finite nonnegative number" % label)
    if not amount.is_finite() or amount < ZERO:
        raise ValueError("%s must be a finite nonnegative number" % label)
    return amount


def _exact_sum(*amounts):
    """Addition/subtraction must not round under the caller's Decimal context."""
    nonzero = [amount for amount in amounts if amount != ZERO]
    if not nonzero:
        return ZERO
    smallest_exponent = min(amount.as_tuple().exponent for amount in nonzero)
    largest_adjusted = max(amount.adjusted() for amount in nonzero)
    with localcontext() as context:
        context.prec = max(1, largest_adjusted - smallest_exponent + len(str(len(nonzero))) + 2)
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        context.clamp = 0
        return sum(nonzero, ZERO)


def _subtract(amount, *deductions):
    return _exact_sum(amount, *(deduction.copy_negate() for deduction in deductions))


def normalize_protected_assets(value):
    """Return a fresh canonical asset -> nonnegative Decimal quantity mapping.

    None means no floors. Case/whitespace aliases of the same asset are rejected,
    because silently selecting one floor would make the protected amount unclear.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("protected_assets must be an asset quantity mapping")
    protected = {}
    for raw_asset, raw_amount in value.items():
        asset = _asset_name(raw_asset)
        if asset in protected:
            raise ValueError("duplicate canonical asset name: %s" % asset)
        protected[asset] = _amount(raw_amount, "protected_assets[%s]" % asset)
    return protected


def validate_policy(max_equity_irt, reinvest_profits, protected_assets):
    """Validate configuration and return its canonical Decimal asset floors.

    Disabling reinvestment requires a positive sleeve cap: it supplies the base
    capital against which realized excess cash can be reserved.
    """
    if not isinstance(reinvest_profits, bool):
        raise ValueError("reinvest_profits must be a boolean")
    cap = None if max_equity_irt is None else _amount(max_equity_irt, "max_equity_irt")
    if not reinvest_profits and (cap is None or cap <= ZERO):
        raise ValueError("reinvest_profits=false requires positive max_equity_irt")
    return normalize_protected_assets(protected_assets)


def _canonical_amount(amount):
    """Compact context-independent decimal encoding, ignoring insignificant zeros."""
    if amount == ZERO:
        return "0"
    _, digits, exponent = amount.as_tuple()
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return "%se%d" % ("".join(str(digit) for digit in digits), exponent)


def policy_fingerprint(cap, reinvest, protected):
    """Stable SHA-256 hex digest of normalized policy settings."""
    floors = validate_policy(cap, reinvest, protected)
    normalized_cap = None if cap is None else _canonical_amount(_amount(cap, "max_equity_irt"))
    payload = [normalized_cap, reinvest,
               [[asset, _canonical_amount(floors[asset])] for asset in sorted(floors)]]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _asset_quantity(value, asset, label):
    if isinstance(value, Mapping):
        # Balance inputs use the same canonical naming and safety checks as floors.
        return normalize_protected_assets(value).get(asset, ZERO)
    return _amount(value, label)


def spendable_asset(asset, total, available, protected, owned=None, committed=None, reserved_irt=0):
    """Return safe free quantity for a base sale or quote purchase.

    total/available/owned/committed may be scalars or asset-keyed mappings; a
    missing mapped asset means zero. Both free and total account balances must
    retain the floor and IRT reserve: frozen balances are never treated as
    satisfying either protection, even when the sleeve ledger claims more cash.

    With owned supplied, spending is also bounded by sleeve ownership less its
    own order commitments and IRT reserve. It never borrows unrelated account
    funds. Without owned, commitments conservatively reduce account
    allowance. reserved_irt applies only when spending IRT.
    """
    asset = _asset_name(asset)
    floors = normalize_protected_assets(protected)
    total = _asset_quantity(total, asset, "total")
    available = _asset_quantity(available, asset, "available")
    commitments = ZERO if committed is None else _asset_quantity(committed, asset, "committed")
    reserve = _amount(reserved_irt, "reserved_irt")
    if asset != "IRT":
        reserve = ZERO
    floor = floors.get(asset, ZERO)
    wallet_allowance = max(ZERO, min(_subtract(total, floor, reserve),
                                    _subtract(available, floor, reserve)))
    if owned is None:
        return max(ZERO, _subtract(wallet_allowance, commitments))
    ownership = _asset_quantity(owned, asset, "owned")
    sleeve_allowance = max(ZERO, _subtract(ownership, commitments, reserve))
    return min(wallet_allowance, sleeve_allowance)


def sweep_profit(cash, reserved, equity, capital, reinvest):
    """Return (new_reserved, signed_active_cash_flow), without changing total cash.

    cash includes the old reserve; equity is CURRENT active equity excluding it.
    With reinvestment disabled, only active IRT cash above base capital is swept.
    A loss never draws from prior reserve. Enabling reinvestment releases prior
    reserve into active cash and reports that release as a positive flow.
    """
    validate_policy(capital, reinvest, {})
    cash = _amount(cash, "cash")
    reserved = _amount(reserved, "reserved")
    equity = _amount(equity, "equity")
    if reserved > cash:
        raise ValueError("reserved profit cannot exceed sleeve cash")
    if reinvest:
        return ZERO, reserved
    capital = _amount(capital, "capital")
    active_cash = _subtract(cash, reserved)
    excess = max(ZERO, _subtract(equity, capital))
    swept = min(active_cash, excess)
    return _exact_sum(reserved, swept), swept.copy_negate() if swept else ZERO
