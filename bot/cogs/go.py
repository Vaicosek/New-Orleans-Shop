"""`/go <code>` -- jump straight to any addressed entity.

The 4-character code is the one place a user types anything by hand, per
CONTRACT.md section 1: it addresses an entity that was already resolved and
minted a code for it (an order, a prediction market, an item's restock
alert, a game round) -- it is never a raw database id.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import addressing
from ..views.orders import build_order_embed, OrderCardView
from ..views.alerts import build_alert_embed, AlertAckView
from ..views import casino as casino_views
from ..views.casino import build_verify_embed, RoundVerifyView
from ..ui.embed import money_text, panel_embed
from .. import queries


class GoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="go", description="Jump to an order, market, item alert or game round by its code.")
    @app_commands.describe(code="The 4-character address code, e.g. from an order or alert card")
    async def go(self, interaction: discord.Interaction, code: str) -> None:
        await interaction.response.defer(ephemeral=True)
        found = addressing.resolve(code)
        if found is None:
            await interaction.followup.send(
                "That code doesn't match anything · check it and try again.", ephemeral=True
            )
            return
        kind, entity_id = found

        if kind == "order":
            await interaction.followup.send(
                embed=build_order_embed(int(entity_id)), view=OrderCardView(), ephemeral=True
            )
        elif kind == "item":
            from core import alerts, catalog

            try:
                item = catalog.get_item(int(entity_id))
                stock = catalog.get_stock(int(entity_id))
            except catalog.NoSuchItem:
                await interaction.followup.send("That item no longer exists.", ephemeral=True)
                return
            # Only show the real restock-alert card, with its live
            # Acknowledge button, when this item is a GENUINE due alert
            # right now (real threshold, actually crossed, not already
            # suppressed) -- `alerts.due()` is the one place that decides
            # that, same as the scheduled scan uses. A fabricated threshold
            # here would print a fake "restock needed" card whose
            # Acknowledge button still genuinely calls `alerts.acknowledge`,
            # silencing the real alert for a condition that was never true.
            due_row = next((d for d in alerts.due() if d["item_id"] == item["id"]), None)
            if due_row is not None:
                await interaction.followup.send(embed=build_alert_embed(due_row), view=AlertAckView(),
                                                 ephemeral=True)
            else:
                body = f"{stock['pieces']} in stock (capacity {stock['capacity']})."
                await interaction.followup.send(embed=panel_embed(item["name"], body), ephemeral=True)
        elif kind == "game_round":
            embed = build_verify_embed(entity_id)
            # `RoundVerifyView` is persistent: its button re-resolves the
            # round from the message FOOTER, never from `self`. This embed
            # renders no footer of its own, so without this the one UI
            # element offered for checking a suspicious result could never
            # resolve anything -- the button answered "could not identify
            # this round" on the very card that was sent to identify it.
            # Same footer `build_result_embed` writes, read by the same
            # `casino.parse_round_id`.
            embed.set_footer(
                text=casino_views._round_footer(entity_id, addressing.mint("game_round", entity_id))
            )
            await interaction.followup.send(embed=embed, view=RoundVerifyView(),
                                             ephemeral=True)
        elif kind == "pred_market":
            market = queries.get_market_detail(int(entity_id))
            if market is None:
                await interaction.followup.send("That market no longer exists.", ephemeral=True)
                return
            body = f"{market['question']}\nStatus: {market['status']}\nPool: {money_text(market['pool'])}"
            await interaction.followup.send(embed=panel_embed("Prediction market", body),
                                             ephemeral=True)
        else:
            await interaction.followup.send("That code's kind isn't recognized.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GoCog(bot))
