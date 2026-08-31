"""New Orleans Discord bot entrypoint.

Boot sequence:
  1. Load config (typed, fails loudly if anything required is missing).
  2. Check the casino's server-seed secret (`games.configure()`) -- fails
     loudly and refuses to start rather than let the casino run on the
     public, insecure default. Same "FATAL, print, re-raise" shape as step 1
     and for the same reason: no shell on this host to read a traceback off.
  3. Apply the schema (idempotent -- CREATE ... IF NOT EXISTS).
  4. Load every cog in its OWN try/except -- one bad cog must never stop the
     other six commands from coming up, because this runs on a Wispbyte
     panel with no shell to fix a broken import by hand.
  5. Register every persistent view exactly once, with no per-message state.
  6. Sync the seven slash commands to the configured guild.
  7. Run a READ-ONLY self-check that resolves every configured guild and
     channel id and prints ONE readable block -- the only diagnostic
     available on a host with no shell.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core import db, games, money, pricing
from core.config import BotConfig, ConfigError, load_bot_config

from .views.alerts import AlertAckView
from core import provision

from .views.casino import RoundVerifyView
from .views.orders import OrderCardView

log = logging.getLogger("nola.bot")

COGS = (
    "bot.cogs.setup",
    "bot.cogs.shop",
    "bot.cogs.orders",
    "bot.cogs.wallet",
    "bot.cogs.casino",
    "bot.cogs.predict",
    "bot.cogs.admin",
    "bot.cogs.go",
)

# Persistent views: registered once at boot with placeholder state. Every
# callback on these re-resolves its subject from the message it fired on --
# `self` here is never trusted for anything beyond "which view class".
PERSISTENT_VIEWS = (OrderCardView, AlertAckView, RoundVerifyView)


class NolaBot(commands.Bot):
    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.nola_config = config

    async def setup_hook(self) -> None:
        for view_cls in PERSISTENT_VIEWS:
            self.add_view(view_cls())

        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("loaded cog %s", cog)
            except Exception:  # noqa: BLE001 -- one bad cog must never stop boot
                log.exception("FAILED to load cog %s -- continuing without it", cog)

        guild = discord.Object(id=self.nola_config.guild_id)
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d slash command(s) to guild %d", len(synced), self.nola_config.guild_id)
        except discord.HTTPException:
            log.exception("slash command sync failed")

    async def on_ready(self) -> None:
        await self._boot_self_check()

    async def _boot_self_check(self) -> None:
        """Read-only. Resolves every configured id and prints ONE block.
        Never writes anything -- a self-check that mutates state on a host
        with no shell to undo it would be its own kind of incident."""
        config = self.nola_config
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("NEW ORLEANS BOT -- BOOT SELF-CHECK")
        lines.append("=" * 60)

        guild = self.get_guild(config.guild_id)
        if guild is None:
            try:
                guild = await self.fetch_guild(config.guild_id)
            except discord.HTTPException as err:
                lines.append(f"GUILD_ID {config.guild_id}: FAIL -- {err}")
                guild = None
        if guild is not None:
            lines.append(f"GUILD_ID {config.guild_id}: OK -- {guild.name!r}")

        # The provisioned layout. On a server where /setup has not run this is
        # the single most useful line in the block -- without it an operator
        # sees a clean-looking check and no explanation for why nothing works.
        if guild is not None:
            live = [c.id for c in guild.channels] + [r.id for r in guild.roles]
            known = provision.stored(guild.id)
            resolved = sum(1 for d in provision.DESIRED
                           if int(known.get(d.key, 0)) in set(live))
            total = len(provision.DESIRED)
            if resolved == total:
                lines.append(f"layout: OK -- {total}/{total} channels and roles provisioned")
            elif resolved == 0:
                lines.append(f"layout: NOT SET UP -- run /setup in {guild.name!r} to build it")
            else:
                lines.append(f"layout: INCOMPLETE -- {resolved}/{total} resolve; "
                             f"run /setup to rebuild the rest")
        else:
            lines.append("layout: SKIPPED -- guild not resolved")

        for env_name, channel_id in config.guild_channel_ids().items():
            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except discord.HTTPException as err:
                    lines.append(f"{env_name} {channel_id}: FAIL -- {err}")
                    continue
            name = getattr(channel, "name", "?")
            lines.append(f"{env_name} {channel_id}: OK -- #{name}")

        for label, role_ids in (("STAFF_ROLE_IDS", config.staff_role_ids),
                                 ("MANAGER_ROLE_IDS", config.manager_role_ids)):
            if not role_ids:
                lines.append(f"{label}: none configured")
                continue
            if guild is None:
                lines.append(f"{label}: SKIPPED -- guild not resolved")
                continue
            for rid in role_ids:
                role = guild.get_role(rid)
                if role is None:
                    lines.append(f"{label} {rid}: FAIL -- role not found in guild")
                else:
                    lines.append(f"{label} {rid}: OK -- {role.name!r}")

        loaded = sorted(self.extensions.keys())
        missing = [c for c in COGS if c not in loaded]
        lines.append(f"cogs loaded: {len(loaded)}/{len(COGS)}"
                     + (f"  MISSING: {', '.join(missing)}" if missing else ""))
        # The restock scan's own health, from its LAST SUCCESSFUL SCAN. A
        # resolved alerts channel says nothing about whether the loop is
        # still alive, and a dead safety system reporting OK is the worst
        # line this block could print.
        _admin_cog = self.get_cog("AdminCog")
        if _admin_cog is not None and hasattr(_admin_cog, "scan_health"):
            lines.append(_admin_cog.scan_health())
        lines.append(f"currency: {pricing.CURRENCY} (gold ingots, whole numbers)")
        lines.extend(_database_lines())
        lines.append("=" * 60)

        print("\n".join(lines), flush=True)


def _database_lines() -> list[str]:
    """Database facts for the boot block.

    There is no shell on the panel, so this print is the only way to see
    whether the database is the one you think it is. Reports its path,
    journal mode, catalog size and treasury balances -- an empty treasury
    is the difference between a working shop and every payout failing, and
    it is invisible until someone tries to get paid.
    """
    out: list[str] = []
    try:
        with db.db() as c:
            items = c.execute("SELECT COUNT(*) n FROM items WHERE active=1").fetchone()["n"]
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
            fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
        out.append(f"database: {db.DB_PATH}")
        out.append(f"  journal={mode}  foreign_keys={'on' if fk else 'OFF'}  items={items}")
        funded = []
        for subject, label in money.TREASURY_NAMES.items():
            bal = money.balance(subject)
            funded.append(f"{label} {bal.coins:,}")
        out.append("  treasuries: " + "  ".join(funded))
        if all(money.balance(s).coins == 0 for s in money.TREASURY_NAMES):
            out.append("  WARNING: every treasury is empty -- order payouts and bets "
                       "will fail until an owner funds one (/admin -> Fund treasury).")
    except Exception as err:                      # never let diagnostics stop a boot
        out.append(f"database: UNREADABLE -- {err}")
    return out


def build_bot() -> NolaBot:
    try:
        config = load_bot_config()
    except ConfigError as err:
        print(f"FATAL: bad configuration -- {err}", flush=True)
        print("Set it in the panel's Startup/Variables tab, then restart.", flush=True)
        raise
    try:
        games.configure()
    except games.SeedSecretError as err:
        print(f"FATAL: {err}", flush=True)
        raise
    db.init_db()
    db.seed_if_empty()
    return NolaBot(config)


def main() -> None:
    """Entry point.

    A misconfiguration exits with the one readable FATAL line and NOTHING
    else. This bot runs on a panel with no shell, so its console output is
    the only diagnostic there is -- and a Python traceback printed under a
    clear instruction buries it, which is exactly the moment the person
    reading needs the instruction most. Real crashes still raise; only the
    two "you have not configured me yet" cases are caught.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        bot = build_bot()
    except (ConfigError, games.SeedSecretError):
        raise SystemExit(1)
    bot.run(bot.nola_config.token, log_handler=None)


if __name__ == "__main__":
    main()
