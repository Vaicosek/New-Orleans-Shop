"""Typed environment access for New Orleans.

No bare `os.getenv` anywhere else in this project -- every value a process
reads from the environment comes through one of the four helpers below, so a
missing or malformed value fails loudly at boot with a readable message
instead of surfacing later as a `None` threaded silently through five call
sites (or a channel id that is actually the string `"None"`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """A required env var is missing or malformed. Always raised at boot,
    never deep inside a request handler."""


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        if required:
            raise ConfigError(f"{name} is required and not set")
        return default
    return value


def env_int(name: str, default: int | None = None, *, required: bool = False) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if required:
            raise ConfigError(f"{name} is required and not set")
        return default
    try:
        return int(raw.strip())
    except ValueError as err:
        raise ConfigError(f"{name}={raw!r} is not an integer") from err


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_ids(name: str, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    """A comma-separated list of Discord snowflakes -- a staff role
    allowlist, a set of manager roles. Never typed one at a time by hand."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError as err:
            raise ConfigError(f"{name} has a non-integer id: {part!r}") from err
    return tuple(ids)


@dataclass(frozen=True)
class BotConfig:
    token: str
    guild_id: int
    shop_channel_id: int
    orders_channel_id: int
    alerts_channel_id: int
    staff_role_ids: tuple[int, ...]
    manager_role_ids: tuple[int, ...]
    command_sync_guild_only: bool

    def guild_channel_ids(self) -> dict[str, int]:
        """Every configured channel id the boot self-check must resolve.
        This dict IS the self-check's worklist -- add a channel here and it
        is verified at every boot with no other code change needed."""
        return {
            "SHOP_CHANNEL_ID": self.shop_channel_id,
            "ORDERS_CHANNEL_ID": self.orders_channel_id,
            "ALERTS_CHANNEL_ID": self.alerts_channel_id,
        }


def load_bot_config() -> BotConfig:
    return BotConfig(
        token=env_str("DISCORD_TOKEN", required=True),
        guild_id=env_int("GUILD_ID", required=True),
        shop_channel_id=env_int("SHOP_CHANNEL_ID", required=True),
        orders_channel_id=env_int("ORDERS_CHANNEL_ID", required=True),
        alerts_channel_id=env_int("ALERTS_CHANNEL_ID", required=True),
        staff_role_ids=env_ids("STAFF_ROLE_IDS"),
        manager_role_ids=env_ids("MANAGER_ROLE_IDS"),
        command_sync_guild_only=env_bool("COMMAND_SYNC_GUILD_ONLY", default=True),
    )
