"""`/admin` -- the only admin-facing slash command, plus the background
loops: the restock-alert scan that posts to `ALERTS_CHANNEL_ID`, the
six-hourly pull of the reference market (CONTRACT.md section 14), and the
one-minute auction sweep that closes and settles any lot whose `closes_at`
has passed.

All three loops live here rather than in their own cogs because this host
gives the project ONE process slot: a loop in a cog that failed to import is
a loop that never runs, and keeping them beside the panel that reports their
health means there is exactly one place to look when something has stopped.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import alerts, auctions, bonds, land, orders as orders_core, refmarket

from .. import layout, loyalty_sync
from ..permissions import is_staff
from ..views.admin import AdminPanelView, build_admin_embed
from ..views.alerts import AlertAckView, build_alert_embed
from ..views.auctions import AuctionCardView, auction_card_view, build_auction_embed
from ..views.land import LandCardView, land_card_view, build_land_embed
from ..views.bonds import BondCardView, bond_card_view, build_bond_embed


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # The health signal is the last SUCCESSFUL scan, not the fact that a
        # channel resolved once at boot.
        self.last_scan_ok_at: datetime | None = None
        self.last_scan_error: str | None = None
        self.scan_alerts.start()
        self.pull_reference_market.start()
        self.sweep_auctions.start()
        self.sweep_land.start()
        self.sweep_bonds.start()
        self.sweep_orders.start()

    def cog_unload(self) -> None:
        self.scan_alerts.cancel()
        self.pull_reference_market.cancel()
        self.sweep_auctions.cancel()
        self.sweep_land.cancel()
        self.sweep_bonds.cancel()
        self.sweep_orders.cancel()

    @app_commands.command(name="admin", description="Items, prices, thresholds, markets, treasury.")
    async def admin(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = getattr(self.bot, "nola_config", None)
        if config is None or not is_staff(interaction.user, config):
            await interaction.followup.send("This panel is staff-only.", ephemeral=True)
            return
        embed = build_admin_embed(config)
        await interaction.followup.send(
            embed=embed, view=AdminPanelView(interaction.user.id, config), ephemeral=True
        )

    @tasks.loop(minutes=10)
    async def scan_alerts(self) -> None:
        """Post every due restock alert. One bad item skips ONE item.

        A single exception escaping this coroutine stops a `tasks.loop` for
        the life of the process -- no DM, no post, no log anyone reads -- so
        the alerting system dies silently while every other command still
        works. The try/except therefore lives INSIDE the loop, and the
        per-scan success time below is what the boot self-check reports.
        """
        config = getattr(self.bot, "nola_config", None)
        if config is None:
            return
        # Through the layout, so this keeps working on a server that was set
        # up by /setup and never had an ALERTS_CHANNEL_ID. None is normal
        # before setup has run -- returning quietly is right, because raising
        # inside a task loop sends the traceback somewhere nobody reads.
        channel = layout.channel(self.bot, config, "channel:alerts")
        if channel is None:
            return
        for due_row in alerts.due():
            try:
                await channel.send(embed=build_alert_embed(due_row), view=AlertAckView())
            except Exception as err:            # noqa: BLE001 -- one item, not the scan
                self.last_scan_error = f"{due_row['item_id']}: {type(err).__name__}: {err}"
                print(f"[alerts] item {due_row['item_id']} failed: {err}", flush=True)
        # Advanced per completed scan, not at startup: the health line below
        # must describe work that actually happened.
        self.last_scan_ok_at = datetime.now(timezone.utc)

    @scan_alerts.error
    async def scan_alerts_error(self, err: BaseException) -> None:
        """Without this the loop dies silently for the life of the process."""
        print(f"[alerts] scan loop crashed: {err!r} -- restarting", flush=True)
        self.last_scan_error = f"loop crashed: {err!r}"
        if not self.scan_alerts.is_running():
            self.scan_alerts.restart()

    def scan_health(self) -> str:
        """One line for the boot self-check. Never says OK on the strength of
        a channel that resolved once at startup -- a dead safety system
        reporting healthy is worse than no report at all."""
        if self.last_scan_ok_at is None:
            return "restock scan: NO SUCCESSFUL SCAN YET" + (
                f" -- last error: {self.last_scan_error}" if self.last_scan_error else "")
        return (f"restock scan: last success {self.last_scan_ok_at:%Y-%m-%d %H:%M:%S} UTC"
                + (f"  (last error: {self.last_scan_error})" if self.last_scan_error else ""))

    @scan_alerts.before_loop
    async def before_scan(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Reference market. Someone else's server, so: six hours, two requests,
    # an honest User-Agent, and no retry on a throttle. See core/refmarket.py.
    # ------------------------------------------------------------------
    @tasks.loop(hours=6)
    async def pull_reference_market(self) -> None:
        """One cycle. `refmarket.pull()` never raises, so this cannot kill the
        loop -- but the try/except stays anyway, because that guarantee lives
        in another file and a loop that dies here dies silently for the life
        of the process."""
        try:
            rows, error = await refmarket.pull()
        except Exception as err:            # noqa: BLE001 -- a feed, not the shop
            print(f"[refmarket] cycle raised: {err!r}", flush=True)
            return
        if error:
            print(f"[refmarket] {error}", flush=True)
        else:
            print(f"[refmarket] {rows} items refreshed from {refmarket.SOURCE}", flush=True)

    @pull_reference_market.error
    async def pull_reference_market_error(self, err: BaseException) -> None:
        print(f"[refmarket] loop crashed: {err!r} -- restarting", flush=True)

    @pull_reference_market.before_loop
    async def before_pull(self) -> None:
        # Deliberately AFTER ready and not at t=0 of the process: a restart
        # loop on this host would otherwise turn into a request every time
        # the container bounces, which is the one thing a polite client of
        # somebody else's API must not do.
        await self.bot.wait_until_ready()


    # ------------------------------------------------------------------
    # Auctions. An auction's outcome is the objective top bid at close, not
    # a staff judgement call the way a prediction market's resolve is, so
    # there is no insider-window reason to make a human close and settle it
    # by hand -- see core/auctions.py's module docstring. One minute is
    # cheap (one indexed SELECT on a local db) and keeps a listed close time
    # honest to within about that margin.
    # ------------------------------------------------------------------
    @tasks.loop(minutes=1)
    async def sweep_auctions(self) -> None:
        """`auctions.sweep_expired()` never raises past a single auction's
        own close/settle -- but the try/except stays anyway, same reasoning
        as every other loop here: a crash inside a `tasks.loop` dies
        silently for the life of the process."""
        try:
            settled_ids = auctions.sweep_expired()
        except Exception as err:            # noqa: BLE001 -- one sweep, not the process
            print(f"[auctions] sweep raised: {err!r}", flush=True)
            return
        for auction_id in settled_ids:
            await self._refresh_auction_card(auction_id)
            await self._sync_winner_rank(auction_id)

    async def _sync_winner_rank(self, auction_id: int) -> None:
        """A settled auction's winner just spent real coins -- that may have
        crossed a loyalty threshold. Best-effort, same as the card refresh
        beside it: a stale rank role is cosmetic, never worth raising over."""
        from .. import queries
        config = getattr(self.bot, "nola_config", None)
        if config is None:
            return
        auction = queries.get_auction_detail(auction_id)
        if auction is None or not auction.get("winner"):
            return
        await loyalty_sync.sync_rank_role(self.bot, config.guild_id, auction["winner"])

    async def _refresh_auction_card(self, auction_id: int) -> None:
        """Edit the public card in place so bidders see the result without
        anyone having to reopen it -- same reasoning as the bid confirm
        gate's own card refresh in bot/views/auctions.py."""
        from .. import queries
        auction = queries.get_auction_detail(auction_id)
        if auction is None or not auction.get("channel_id") or not auction.get("message_id"):
            return
        try:
            channel = self.bot.get_channel(int(auction["channel_id"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(auction["channel_id"]))
            message = await channel.fetch_message(int(auction["message_id"]))
            await message.edit(embed=build_auction_embed(auction_id),
                                view=auction_card_view(auction))
        except discord.HTTPException as err:
            print(f"[auctions] could not refresh card for auction {auction_id}: {err!r}", flush=True)

    @sweep_auctions.error
    async def sweep_auctions_error(self, err: BaseException) -> None:
        print(f"[auctions] sweep loop crashed: {err!r} -- restarting", flush=True)

    @sweep_auctions.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Land. Same reasoning as the auction sweep above -- a listing's winner
    # is the objective top bid at close (or whoever cleared buy-now, which
    # settles itself instantly inside land.bid), never a staff call.
    # ------------------------------------------------------------------
    @tasks.loop(minutes=1)
    async def sweep_land(self) -> None:
        """`land.sweep_expired()` never raises past a single listing's own
        close/settle -- same try/except shape as `sweep_auctions`."""
        try:
            settled_ids = land.sweep_expired()
        except Exception as err:            # noqa: BLE001 -- one sweep, not the process
            print(f"[land] sweep raised: {err!r}", flush=True)
            return
        for land_id in settled_ids:
            await self._refresh_land_card(land_id)
            await self._sync_land_winner_rank(land_id)

    async def _sync_land_winner_rank(self, land_id: int) -> None:
        """Same reasoning as `_sync_winner_rank`: a settled listing's
        winner just spent real coins, best-effort loyalty-rank sync."""
        from .. import queries
        config = getattr(self.bot, "nola_config", None)
        if config is None:
            return
        listing = queries.get_land_detail(land_id)
        if listing is None or not listing.get("winner"):
            return
        await loyalty_sync.sync_rank_role(self.bot, config.guild_id, listing["winner"])

    async def _refresh_land_card(self, land_id: int) -> None:
        """Same reasoning as `_refresh_auction_card`."""
        from .. import queries
        listing = queries.get_land_detail(land_id)
        if listing is None or not listing.get("channel_id") or not listing.get("message_id"):
            return
        try:
            channel = self.bot.get_channel(int(listing["channel_id"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(listing["channel_id"]))
            message = await channel.fetch_message(int(listing["message_id"]))
            await message.edit(embed=build_land_embed(land_id),
                                view=land_card_view(listing))
        except discord.HTTPException as err:
            print(f"[land] could not refresh card for listing {land_id}: {err!r}", flush=True)

    @sweep_land.error
    async def sweep_land_error(self, err: BaseException) -> None:
        print(f"[land] sweep loop crashed: {err!r} -- restarting", flush=True)

    @sweep_land.before_loop
    async def before_sweep_land(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Bonds. A 15-minute cadence, not 1: coupons and maturities land on a
    # day/month scale, not a minute one, so unlike the auction/land sweeps
    # there is no user-visible reason to check every minute -- see
    # core/bonds.py's `sweep_expired` for the catch-up-gradually reasoning
    # this cadence implies.
    # ------------------------------------------------------------------
    @tasks.loop(minutes=15)
    async def sweep_bonds(self) -> None:
        """`bonds.sweep_expired()` never raises past a single bond's own
        coupon/maturity -- same try/except shape as the other sweeps."""
        try:
            result = bonds.sweep_expired()
        except Exception as err:            # noqa: BLE001 -- one sweep, not the process
            print(f"[bonds] sweep raised: {err!r}", flush=True)
            return
        for bond_id in {*result["coupons_paid"], *result["matured"]}:
            await self._refresh_bond_card(bond_id)

    async def _refresh_bond_card(self, bond_id: int) -> None:
        """Same reasoning as `_refresh_auction_card`/`_refresh_land_card`."""
        from .. import queries
        bond = queries.get_bond_detail(bond_id)
        if bond is None or not bond.get("channel_id") or not bond.get("message_id"):
            return
        try:
            channel = self.bot.get_channel(int(bond["channel_id"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(bond["channel_id"]))
            message = await channel.fetch_message(int(bond["message_id"]))
            await message.edit(embed=build_bond_embed(bond_id), view=bond_card_view(bond))
        except discord.HTTPException as err:
            print(f"[bonds] could not refresh card for bond {bond_id}: {err!r}", flush=True)

    @sweep_bonds.error
    async def sweep_bonds_error(self, err: BaseException) -> None:
        print(f"[bonds] sweep loop crashed: {err!r} -- restarting", flush=True)

    @tasks.loop(minutes=30)
    async def sweep_orders(self) -> None:
        """Bounties on unclaimed work and customer deadlines
        (`orders.sweep_stale`), then stall rent (`land.sweep_rent`). Both
        are idempotent per period, so the interval is about how quickly a
        bump or a vacate becomes visible, not about correctness."""
        try:
            result = orders_core.sweep_stale()
        except Exception as err:            # noqa: BLE001
            print(f"[orders] stale sweep raised: {err!r}", flush=True)
            result = {"cancelled": [], "bumped": []}
        for order_id in {*result["cancelled"], *result["bumped"]}:
            await self._refresh_order_card(order_id)
        try:
            rent = land.sweep_rent()
        except Exception as err:            # noqa: BLE001
            print(f"[land] rent sweep raised: {err!r}", flush=True)
            return
        for land_id in {*rent["charged"], *rent["vacated"]}:
            await self._refresh_land_card(land_id)

    async def _refresh_order_card(self, order_id: int) -> None:
        """A bumped bounty or an expired order has to show on the public
        card, or the sweep changed a number nobody can see."""
        from ..views.orders import OrderCardView, build_order_embed
        try:
            order = orders_core.get_order(order_id)
        except Exception:  # noqa: BLE001
            return
        if not order.get("channel_id") or not order.get("message_id"):
            return
        try:
            channel = self.bot.get_channel(int(order["channel_id"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(order["channel_id"]))
            message = await channel.fetch_message(int(order["message_id"]))
            await message.edit(embed=build_order_embed(order_id), view=OrderCardView())
        except discord.HTTPException as err:
            print(f"[orders] could not refresh card for order {order_id}: {err!r}", flush=True)

    @sweep_orders.error
    async def sweep_orders_error(self, err: BaseException) -> None:
        print(f"[orders] sweep loop crashed: {err!r} -- restarting", flush=True)

    @sweep_bonds.before_loop
    async def before_sweep_bonds(self) -> None:
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
