"""Shared embed helpers: row layout, money formatting, price rendering.

`price_row`/`price_line` are the ONLY sanctioned way anything under `bot/`
turns an item's stored price into display text -- both call
`core.pricing.price_label()`, which always states both bases (stack and
piece). Nothing here divides a price itself; that math lives in exactly one
place, `core/pricing.py`, per CONTRACT.md section 5.

There is exactly one currency in this shop -- gold ingots, whole numbers,
symbol `g` (see `core.pricing.CURRENCY`). `money_text` renders every bare
amount with that same symbol; nothing under `bot/` may pass a different
name in, because a second currency word next to `price_label`'s `g` is how
"1,450 coins" ends up two panels away from "1 g" for the same money.
"""
from __future__ import annotations

import discord

from core import pricing
from core.pricing import price_label

# The colour bar is the only thing in an embed that a person reads before the
# words, so it has to SAY something. One colour on every panel says "this bot
# has a brand" and nothing else, which is the same as saying nothing -- and it
# is the single most reliable tell of a generated bot.
#
# Five tones, each tied to a meaning a reader acts on:
#   neutral  no bar at all. A board, a list, a lookup -- information, no verdict.
#   brand    the flag's gold. Official statements the shop is making: the
#            storefront, the admin panel, a settlement.
#   gain     money arrived.
#   loss     it failed, it was cancelled, or money left.
#   warn     something needs attention but nothing has gone wrong yet.
#
# Neutral is the DEFAULT and should stay the commonest. A page of coloured bars
# is the rainbow problem wearing a semantic disguise.
# Built from ints via discord.Color, not Colour.from_str(): the constructor
# does not exist in every version this may run under, and the British spelling
# is an alias the test stub does not carry. An int and the US spelling work
# everywhere. The literals are the site's own tokens.
TONES: "dict[str, object | None]" = {
    "neutral": None,
    "brand":   discord.Color(0xD9B544),   # the site's --accent
    "gain":    discord.Color(0x7FB56A),
    "loss":    discord.Color(0xD0503A),   # the flag's red, muted
    "warn":    discord.Color(0xC98A3A),
}

EMBED_COLOR = TONES["brand"]   # kept: older call sites import this by name
SEP = "·"  # ' · ' joins fields on one row -- never ASCII '--'.


def rows(lines: list[str], *, empty_text: str = "") -> str:
    """Join row strings into one embed body. An empty list stays empty --
    CONTRACT.md section 7: no placeholder rows, no decorated absence. Pass
    `empty_text` only when the caller wants exactly one plain sentence
    (e.g. "No open orders."), never a styled placeholder block."""
    if not lines:
        return empty_text
    return "\n".join(lines)


def money_text(amount: int) -> str:
    """Render a bare amount in the shop's one currency. No name parameter --
    an earlier version took `currency_name="coin"` and every call site had
    to remember to override it, which is how a panel ends up printing
    "coins" while the price list next to it prints "g" for the same money.

    One line, because the formatter itself now lives in `core.pricing` --
    `web/` needs the same text and cannot import this module (it imports
    discord). Output is byte-identical, and `pricing._require_int` keeps the
    same TypeError for a bool or a non-int.
    """
    return pricing.money_text(amount)


def price_line(name: str, price_coins: int, price_unit_pieces: int,
               stack_size: int | None = None) -> str:
    """One catalog row: name, then both price bases. Never a bare number.

    `price_unit_pieces` (never `stack_size`) is the divisor for the
    per-piece figure -- `stack_size` is passed through only so the label can
    say "stack of N" when the quote unit happens to be a full stack.
    """
    return f"{name} {SEP} {price_label(price_coins, price_unit_pieces, stack_size)}"


def price_only(price_coins: int, price_unit_pieces: int,
               stack_size: int | None = None) -> str:
    """Just the price text, both bases, for embedding inline in a longer
    sentence rather than a full catalog row."""
    return price_label(price_coins, price_unit_pieces, stack_size)


def panel_embed(title: str, description: str = "", *, footer: str | None = None,
                tone: str = "neutral") -> discord.Embed:
    """One line if it can be one line; no emoji in the title -- CONTRACT.md
    section 7. Callers pass plain text; this does not scrub emoji for them,
    it just never adds any of its own.

    `tone` picks the colour bar from TONES above. It defaults to neutral --
    no bar -- because most panels are information and a colour that means
    nothing is worse than no colour. An unknown tone falls back to neutral
    rather than raising: a typo in a decorative argument must never be able
    to take down a panel somebody is waiting on.
    """
    e = discord.Embed(title=title, description=description,
                      color=TONES.get(tone))
    if footer:
        e.set_footer(text=footer)
    return e
