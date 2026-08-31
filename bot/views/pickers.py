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
                "This picker isn't yours · open your own with the panel's button.",
                ephemeral=True,
            )
            return False
        return True


class ItemPickerView(_OwnerGuardedView):
    """Step 1 for any flow keyed on an item: real catalog search results in
    a Select. `on_picked` receives the full item dict from `catalog.get_item`
    -- never a bare id passed onward for someone else to re-look-up.

    A Select tops out at 25 options. When the unfiltered/un-categorised
    result would blow past that, this inserts one extra narrowing step --
    a Select of categories (from `catalog.categories_with_items`, so the
    counts reflect the same `active_only` filter as the item search) --
    ahead of the item Select, rather than silently truncating the catalog
    to its first 25 rows. `category` is how that second step re-enters the
    constructor once narrowed; callers driving the flow fresh never pass it.
    """

    _MAX_OPTIONS = 25

    def __init__(self, owner_id: int, on_picked: OnPicked, *,
                 search_term: str = "", active_only: bool = True, timeout: float = 180,
                 category: str | None = None):
        super().__init__(owner_id, timeout)
        self._on_picked = on_picked
        self._search_term = search_term
        self._active_only = active_only
        self._timeout = timeout
        self._category = category

        if category is None:
            # Cheap over-the-cap probe: one extra row above the limit is
            # enough to know truncation would happen, without pulling the
            # whole table just to find out.
            probe = catalog.search(search_term, active_only=active_only,
                                    limit=self._MAX_OPTIONS + 1)
            if len(probe) > self._MAX_OPTIONS:
                self._build_category_step()
                return
            items = probe
        else:
            # Re-entered after a category was picked: search normally, then
            # narrow to that category. `catalog.search` has no category
            # parameter of its own, so this pulls a generous batch and
            # filters here -- a category is one narrowing step, expected to
            # land comfortably under the cap on its own.
            items = [
                it for it in catalog.search(search_term, active_only=active_only, limit=1000)
                if (it.get("category") or "Uncategorized") == category
            ][: self._MAX_OPTIONS]

        self._build_item_select(items)

    def _build_category_step(self) -> None:
        cats = catalog.categories_with_items(active_only=self._active_only)
        term = (self._search_term or "").strip().lower()

        def _matches(it: dict) -> bool:
            return not term or term in it["name"].lower()

        options: list[discord.SelectOption] = []
        for cat in cats:
            count = sum(1 for it in cat["items"] if _matches(it))
            if count:
                options.append(discord.SelectOption(
                    label=(cat["name"] or "Uncategorized")[:100],
                    value=(cat["name"] or "Uncategorized")[:100],
                    description=f"{count} item{'s' if count != 1 else ''}",
                ))
        options = options[: self._MAX_OPTIONS]

        select: discord.ui.Select = discord.ui.Select(
            placeholder="Too many matches -- choose a category..." if options else "No items match",
            min_values=1,
            max_values=1,
            options=(options or [discord.SelectOption(label="No items available", value="_none")]),
            disabled=not options,
        )

        async def _category_picked(interaction: discord.Interaction) -> None:
            narrowed = ItemPickerView(
                self.owner_id, self._on_picked,
                search_term=self._search_term, active_only=self._active_only,
                timeout=self._timeout, category=select.values[0],
            )
            await interaction.response.edit_message(view=narrowed)

        select.callback = _category_picked
        self.add_item(select)

    def _build_item_select(self, items: list[dict]) -> None:
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
                # Both truncated to Discord's 100-char limit -- a label
                # over the limit just gets cut, but an over-limit VALUE
                # makes discord.py refuse to build the component at all,
                # which is how a market with one long outcome name became
                # unstakeable and unresolvable: the picker never sent.
                [discord.SelectOption(label=label[:100], value=value[:100])
                 for label, value in options]
                or [discord.SelectOption(label="Nothing available", value="_none")]
            ),
            disabled=not options,
        )

        async def _picked(interaction: discord.Interaction) -> None:
            await self._on_picked(interaction, select.values[0])

        select.callback = _picked
        self.add_item(select)
