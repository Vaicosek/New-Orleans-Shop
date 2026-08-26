"""Prices. Integers only, one rounding rule, one place.

The currency is GOLD INGOTS and it is a whole number. There are no fractional
gold, so nothing here ever needs a decimal type -- the only fractions in the
system are per-piece figures derived for DISPLAY, and those never come back
into a calculation.

The 64x confusion is the most repeated bug in the economy this is modelled on,
and the cause is storing a DERIVED number. That system stores a float price per
piece and converts on input with `price / 64.0`. 300 / 64 = 4.6875, which is not
a whole coin, so floats propagate everywhere and different call sites round
differently.

Here the stored number is the one the owner typed -- the price of a full stack --
and per-piece figures are derived, with integer arithmetic, by exactly one
function. Nothing else in the codebase may divide a price.
"""
from __future__ import annotations

from typing import Final

STACK: Final[int] = 64

# Gold ingots. Whole numbers, no fractional unit exists.
CURRENCY: Final[str] = "g"


def charge(pieces: int, price_coins: int, unit_pieces: int = STACK) -> int:
    """What `pieces` pieces cost, in whole coins. Half-up, integer maths only.

    >>> charge(64, 300)       # one full stack
    300
    >>> charge(1, 300)        # 4.6875 -> 5
    5
    >>> charge(32, 300)       # exactly 150
    150
    >>> charge(0, 300)
    0
    """
    _require_int(pieces, "pieces")
    _require_int(price_coins, "price_coins")
    _require_int(unit_pieces, "unit_pieces")
    if pieces < 0:
        raise ValueError("pieces must not be negative")
    if price_coins < 0:
        raise ValueError("price must not be negative")
    if unit_pieces <= 0:
        raise ValueError("unit_pieces must be positive")
    # half-up without touching a float: (a/b) rounded = (2a + b) // (2b)
    numerator = pieces * price_coins * 2 + unit_pieces
    return numerator // (unit_pieces * 2)


def split_charge(pieces_per_claim: list[int], price_coins: int,
                 unit_pieces: int = STACK) -> list[int]:
    """Split `charge(sum(pieces_per_claim), ...)` across N claims so the
    payouts SUM to exactly the single-claim charge, whatever the split.

    WHY THIS EXISTS -- the claim-fragmentation overpay: `charge()` rounds
    half-up, once. Rounding a total once and rounding each fragment of it
    separately are not the same operation the moment more than one fragment
    is involved: summing N independently-rounded fragments does not equal
    rounding the sum. `order_claims` is UNIQUE(order_id, worker), so a single
    worker cannot split their own claim -- but nothing stops several
    colluding or sock-puppet accounts from each claiming a slice of the same
    order, and multiple claimants on one order is this shop's NORMAL case,
    not an exotic one. A 64-piece order at 300/stack pays 300 as one claim
    of 64, but 320 as sixty-four 1-piece claims (charge(1, 300, 64) rounds
    4.6875 up to 5, and 64 * 5 = 320) -- a 6.67% leak that scales with
    however many accounts one person controls.

    The fix is cumulative differencing: sort claims into one stable,
    deterministic order (the caller's job -- e.g. by claimed_at/id), and pay
    claim i `charge(cum_i) - charge(cum_{i-1})`, where `cum_i` is pieces
    delivered through claim i. This telescopes exactly to
    `charge(sum(pieces_per_claim), ...)` regardless of how the pieces are
    split, and because `charge()` is non-decreasing in pieces, every payout
    is >= 0 -- no claim is ever asked to give money back.
    """
    _require_int(unit_pieces, "unit_pieces")
    if unit_pieces <= 0:
        raise ValueError("unit_pieces must be positive")
    payouts: list[int] = []
    cumulative_pieces = 0
    previous_charge = 0
    for pieces in pieces_per_claim:
        _require_int(pieces, "pieces")
        if pieces < 0:
            raise ValueError("pieces must not be negative")
        cumulative_pieces += pieces
        running_charge = charge(cumulative_pieces, price_coins, unit_pieces)
        payouts.append(running_charge - previous_charge)
        previous_charge = running_charge
    return payouts


def per_piece_text(price_coins: int, unit_pieces: int = STACK) -> str:
    """The per-piece figure as display text, two decimals, for labelling only.

    Never feed this back into a calculation -- `charge()` is the only thing that
    decides what someone pays.
    """
    _require_int(price_coins, "price_coins")
    _require_int(unit_pieces, "unit_pieces")
    if unit_pieces <= 0:
        raise ValueError("unit_pieces must be positive")
    hundredths = (price_coins * 100 * 2 + unit_pieces) // (unit_pieces * 2)
    whole, rem = divmod(hundredths, 100)
    # A price sheet prints 1,500 -- not 1500.00. Separators on both bases or the
    # eye cannot compare the two numbers sitting next to each other in a row.
    return f"{whole:,}" if rem == 0 else f"{whole:,}.{rem:02d}"


def price_label(price_coins: int, unit_pieces: int = STACK,
                stack_size: int | None = None) -> str:
    """Both bases, always. A bare price number is never shown to a user.

    `unit_pieces` is how the price is QUOTED -- 64 for "1g/stack", 32 for
    "1g/32", 1 for "3g/each". `stack_size` is the Minecraft stack size and is
    named in the label only when the quote unit happens to be a full stack, so
    "1 g / stack of 64" reads as it does on the shop sign while "1 g / 32"
    reads as it does on the price list.

    >>> price_label(300, 64, 64)
    '300 g / stack of 64  ·  4.69 g / piece'
    >>> price_label(1, 32, 64)
    '1 g / 32  ·  0.03 g / piece'
    >>> price_label(3, 1, 1)
    '3 g each'
    """
    _require_int(price_coins, "price_coins")
    _require_int(unit_pieces, "unit_pieces")
    if unit_pieces <= 0:
        raise ValueError("unit_pieces must be positive")
    if unit_pieces == 1:
        return f"{price_coins:,} {CURRENCY} each"
    unit = (f"stack of {unit_pieces}" if stack_size is not None and unit_pieces == stack_size
            else str(unit_pieces))
    return (f"{price_coins:,} {CURRENCY} / {unit}"
            f"  ·  {per_piece_text(price_coins, unit_pieces)} {CURRENCY} / piece")


def _require_int(value: object, name: str) -> None:
    # bool is an int subclass and has no business being money or a quantity.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
