"""The reusable View(Select|UserSelect) -> Modal two-step.

A Modal can only hold `discord.ui.TextInput`; it cannot autocomplete and
cannot hold a Select. A View cannot hold free text. So identity is always
resolved in a View first -- a Select built from a real query, or Discord's
own UserSelect -- and only the ALREADY-RESOLVED object is handed to the
Modal that follows. Nothing under `bot/` may ask a user to type a user id,
item id, order id or market id into a text field; this module is the one
place that pattern is implemented, so every panel gets it for free instead
of reinventing it slightly differently each time.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import discord

from core import catalog

OnPicked = Callable[[discord.Interaction, Any], Awaitable[None]]


class _OwnerGuardedView(discord.ui.View):
    """Only the person who opened the picker may use it."""

    def __init__(self, owner_id: int, timeout: float | None):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This picker isn't yours -- open your own with the panel's button.",
                ephemeral=True,
            )
            return False
        return True


class ItemPickerView(_OwnerGuardedView):
    """Step 1 for any flow keyed on an item: real catalog search results in
    a Select. `on_picked` receives the full item dict from `catalog.get_item`
    -- never a bare id passed onward for someone else to re-look-up."""

    def __init__(self, owner_id: int, on_picked: OnPicked, *,
                 search_term: str = "", active_only: bool = True, timeout: float = 180):
        super().__init__(owner_id, timeout)
        self._on_picked = on_picked
        items = catalog.search(search_term, active_only=active_only, limit=25)

        def _option(it: dict) -> discord.SelectOption:
            # Category/subcategory goes in `description`, never crammed into
            # the label -- with Wood alone spanning Logs/Leaves/Saplings, a
            # label of just "Oak Sapling" can otherwise sit right next to
            # "Oak Log" with nothing to tell them apart at a glance.
            bits = [b for b in (it.get("category"), it.get("subcategory")) if b]
            description = " \u2014 ".join(bits)[:100] or None
            return discord.SelectOption(
                label=it["label"][:100], value=str(it["id"]), description=description
            )

        select: discord.ui.Select = discord.ui.Select(
            placeholder="Choose an item..." if items else "No items match",
            min_values=1,
            max_values=1,
            options=(
                [_option(it) for it in items]
                or [discord.SelectOption(label="No items available", value="_none")]
            ),
            disabled=not items,
        )

        async def _picked(interaction: discord.Interaction) -> None:
            item = catalog.get_item(int(select.values[0]))
            await self._on_picked(interaction, item)

        select.callback = _picked
        self.add_item(select)


class UserPickerView(_OwnerGuardedView):
    """Step 1 for any flow keyed on a Discord member: Discord's own
    type-to-search picker. `on_picked` receives the resolved `discord.Member`
    (or `discord.User` in a DM context) -- never an id typed by hand."""

    def __init__(self, owner_id: int, on_picked: OnPicked, *,
                 placeholder: str = "Search for a member...", timeout: float = 180):
        super().__init__(owner_id, timeout)
        self._on_picked = on_picked
        select = discord.ui.UserSelect(placeholder=placeholder, min_values=1, max_values=1)

        async def _picked(interaction: discord.Interaction) -> None:
            await self._on_picked(interaction, select.values[0])

        select.callback = _picked
        self.add_item(select)


class OptionPickerView(_OwnerGuardedView):
    """Step 1 for picking among a small closed set of labelled values -- a
    game, a bet selection, an order or market from a short in-memory list.
    Still a Select, never a text field, even with no database search behind
    it: the rule is about typed identity, not about where the options live.
    """

    def __init__(self, owner_id: int, options: list[tuple[str, str]], on_picked: OnPicked, *,
                 placeholder: str = "Choose...", timeout: float = 180):
        super().__init__(owner_id, timeout)
        self._on_picked = on_picked
        select = discord.ui.Select(
            placeholder=placeholder if options else "Nothing to choose from",
            min_values=1,
            max_values=1,
            options=(
                [discord.SelectOption(label=label[:100], value=value) for label, value in options]
                or [discord.SelectOption(label="Nothing available", value="_none")]
            ),
            disabled=not options,
        )

        async def _picked(interaction: discord.Interaction) -> None:
            await self._on_picked(interaction, select.values[0])

        select.callback = _picked
        self.add_item(select)
