"""Shared embed helpers: row layout, money formatting, price rendering.

`price_row`/`price_line` are the ONLY sanctioned way anything under `bot/`
turns an item's stored price into display text -- both call
`core.pricing.price_label()`, which always states both bases (stack and
piece). Nothing here divides a price itself; that math lives in exactly one
place, `core/pricing.py`, per CONTRACT.md section 5.
"""
from __future__ import annotations

import discord

from core.pricing import price_label

EMBED_COLOR = discord.Color.dark_gold()


def rows(lines: list[str], *, empty_text: str = "") -> str:
    """Join row strings into one embed body. An empty list stays empty --
    CONTRACT.md section 7: no placeholder rows, no decorated absence. Pass
    `empty_text` only when the caller wants exactly one plain sentence
    (e.g. "No open orders."), never a styled placeholder block."""
    if not lines:
        return empty_text
    return "\n".join(lines)


def money_text(amount: int, currency_name: str = "coin") -> str:
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError("amount must be an int")
    unit = currency_name if abs(amount) == 1 else f"{currency_name}s"
    return f"{amount:,} {unit}"


def price_line(name: str, price_coins: int, price_unit_pieces: int,
               stack_size: int | None = None) -> str:
    """One catalog row: name, then both price bases. Never a bare number.

    `price_unit_pieces` (never `stack_size`) is the divisor for the
    per-piece figure -- `stack_size` is passed through only so the label can
    say "stack of N" when the quote unit happens to be a full stack.
    """
    return f"{name} -- {price_label(price_coins, price_unit_pieces, stack_size)}"


def price_only(price_coins: int, price_unit_pieces: int,
               stack_size: int | None = None) -> str:
    """Just the price text, both bases, for embedding inline in a longer
    sentence rather than a full catalog row."""
    return price_label(price_coins, price_unit_pieces, stack_size)


def panel_embed(title: str, description: str = "", *, footer: str | None = None) -> discord.Embed:
    """One line if it can be one line; no emoji in the title -- CONTRACT.md
    section 7. Callers pass plain text; this does not scrub emoji for them,
    it just never adds any of its own."""
    e = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    if footer:
        e.set_footer(text=footer)
    return e
