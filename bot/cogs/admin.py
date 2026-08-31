"""`/admin` -- the only admin-facing slash command, plus the two background
loops: the restock-alert scan that posts to `ALERTS_CHANNEL_ID`, and the
six-hourly pull of the reference market (CONTRACT.md section 13).

Both loops live here rather than in their own cogs because this host gives the
project ONE process slot: a loop in a cog that failed to import is a loop that
never runs, and keeping them beside the panel that reports their health means
there is exactly one place to look when something has stopped.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import alerts, refmarket

from .. import layout
from ..permissions import is_staff
from ..views.admin import AdminPanelView, build_admin_embed
from ..views.alerts import AlertAckView, build_alert_embed


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # The health signal is the last SUCCESSFUL scan, not the fact that a
        # channel resolved once at boot.
        self.last_scan_ok_at: datetime | None = None
        self.last_scan_error: str | None = None
        self.scan_alerts.start()
        self.pull_reference_market.start()

    def cog_unload(self) -> None:
        self.scan_alerts.cancel()
        self.pull_reference_market.cancel()

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
