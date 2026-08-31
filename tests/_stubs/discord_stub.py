"""A throwaway `discord` stub so `bot/` can be IMPORTED and statically
inspected without the real discord.py or a network connection.

Modelled on AbexTech's `_harness/stubs.py`: register fake modules in
`sys.modules` before anything under `bot/` is imported. Every module under
`bot/` starts with `from __future__ import annotations`, so type hints are
never evaluated at runtime -- this stub only needs to cover names that are
actually CALLED or SUBCLASSED, not every name that appears in an annotation.
"""
from __future__ import annotations

import types


def install() -> None:
    discord = types.ModuleType("discord")

    class Color:
        def __init__(self, value: int = 0):
            self.value = value

        @classmethod
        def dark_gold(cls) -> "Color":
            return cls(0xC9B37A)

    class ButtonStyle:
        primary = 1
        secondary = 2
        success = 3
        danger = 4
        # A link button has no callback -- Discord opens the URL itself. The
        # stub carries it because bot/views/shop.py uses one, and a stub that
        # is missing a member the real library has turns a working panel into
        # a test-only AttributeError.
        link = 5

    class Embed:
        def __init__(self, title: str = "", description: str = "", color=None):
            self.title = title
            self.description = description
            self.color = color
            self._footer_text: str | None = None
            self.fields: list[dict] = []

        def set_footer(self, *, text: str | None = None, **_kw) -> None:
            self._footer_text = text

        @property
        def footer(self):
            return types.SimpleNamespace(text=self._footer_text)

        def add_field(self, **kw) -> None:
            self.fields.append(kw)

    class SelectOption:
        def __init__(self, *, label: str, value: str, **_kw):
            self.label, self.value = label, value

    class Intents:
        def __init__(self):
            self.message_content = False

        @classmethod
        def default(cls) -> "Intents":
            return cls()

    class Object:
        def __init__(self, id: int):
            self.id = id

    class HTTPException(Exception):
        pass

    class Interaction:
        pass

    class Message:
        pass

    class Member:
        pass

    class User:
        pass


    class _Item:
        def __init__(self, *a, **kw):
            self.kw = kw
            for k, v in kw.items():
                setattr(self, k, v)
            self.callback = None

    class TextInput(_Item):
        def __init__(self, *, label: str = "", placeholder=None, default=None,
                     max_length=None, required=True, style=None, **kw):
            super().__init__(label=label, placeholder=placeholder, default=default,
                              max_length=max_length, required=required, style=style, **kw)
            self.value = default or ""

    class Button(_Item):
        pass

    class Select(_Item):
        def __init__(self, *, options=None, **kw):
            super().__init__(options=options or [], **kw)
            self.values: list[str] = []

    class UserSelect(_Item):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.values: list = []

    class View:
        def __init__(self, *a, timeout: float | None = 180, **kw):
            self.timeout = timeout
            self.children: list = []

        def add_item(self, item) -> None:
            self.children.append(item)

        async def interaction_check(self, interaction) -> bool:  # pragma: no cover
            return True

    class _ModalMeta(type):
        def __new__(mcls, name, bases, ns, **kw):
            cls = super().__new__(mcls, name, bases, ns)
            cls.__modal_title__ = kw.get("title")
            return cls

        def __init__(cls, name, bases, ns, **kw):
            super().__init__(name, bases, ns)

    class Modal(metaclass=_ModalMeta):
        def __init__(self, *a, title: str | None = None, timeout: float | None = 180, **kw):
            self.title = title or getattr(type(self), "__modal_title__", None)
            self.timeout = timeout
            self.children: list = []

        def add_item(self, item) -> None:
            self.children.append(item)

        async def on_submit(self, interaction) -> None:  # pragma: no cover
            pass

    def button(*_a, **_kw):
        def deco(func):
            func.__discord_ui_kind__ = "button"
            func.__discord_ui_kwargs__ = _kw
            return func
        return deco

    def select(*_a, **_kw):
        def deco(func):
            func.__discord_ui_kind__ = "select"
            func.__discord_ui_kwargs__ = _kw
            return func
        return deco

    ui = types.ModuleType("discord.ui")
    ui.View, ui.Modal, ui.TextInput = View, Modal, TextInput
    ui.Button, ui.Select, ui.UserSelect = Button, Select, UserSelect
    ui.Item = _Item
    ui.button, ui.select = button, select

    discord.ui = ui
    discord.Embed = Embed
    discord.Color = Color
    discord.ButtonStyle = ButtonStyle
    discord.SelectOption = SelectOption
    discord.Intents = Intents
    discord.Object = Object
    discord.HTTPException = HTTPException
    discord.Interaction = Interaction
    discord.Message = Message
    discord.Member = Member
    discord.User = User
    discord.abc = types.SimpleNamespace(User=User)

    # ---- discord.app_commands ----
    app_commands = types.ModuleType("discord.app_commands")

    class _AppCommand:
        """Stands in for the real Command object a `@app_commands.command`
        decorator produces. `callback` is the untouched original coroutine
        function, so the surface test can inspect its real source."""

        def __init__(self, callback, *, name: str | None = None, description: str = ""):
            self.callback = callback
            self.name = name or callback.__name__
            self.description = description

    def command(*, name: str | None = None, description: str = ""):
        def deco(func):
            return _AppCommand(func, name=name, description=description)
        return deco

    def describe(*_a, **_kw):
        def deco(func):
            return func
        return deco

    def guild_only(*_a, **_kw):
        """Pass-through. The real decorator makes Discord refuse the command
        in DMs; there is nothing to simulate here, but a cog using it must
        still import, and a missing name reads as a broken module."""
        def deco(func):
            return func
        return deco

    app_commands.command = command
    app_commands.describe = describe
    app_commands.guild_only = guild_only
    app_commands.AppCommand = _AppCommand
    discord.app_commands = app_commands

    # ---- discord.ext.commands / discord.ext.tasks ----
    ext = types.ModuleType("discord.ext")

    commands_mod = types.ModuleType("discord.ext.commands")

    class Cog:
        pass

    class Bot:
        def __init__(self, *a, command_prefix=None, intents=None, **kw):
            self.command_prefix = command_prefix
            self.intents = intents
            self.extensions: dict = {}
            self.tree = types.SimpleNamespace(
                copy_global_to=lambda **_kw: None,
                sync=_async_noop_list,
            )

        async def load_extension(self, name: str) -> None:
            self.extensions[name] = True

        async def add_cog(self, cog) -> None:
            pass

        def add_view(self, view) -> None:
            pass

        def get_channel(self, _id):
            return None

        async def fetch_channel(self, _id):
            raise HTTPException("not available in tests")

        def get_guild(self, _id):
            return None

        async def fetch_guild(self, _id):
            raise HTTPException("not available in tests")

        async def wait_until_ready(self) -> None:
            return None

        def run(self, *a, **kw) -> None:  # pragma: no cover
            pass

    commands_mod.Cog = Cog
    commands_mod.Bot = Bot

    tasks_mod = types.ModuleType("discord.ext.tasks")

    class _Loop:
        def __init__(self, coro, **_kw):
            self.coro = coro
            self._before = None
            self._error = None
            self._running = False

        def __get__(self, obj, objtype=None):
            return self

        def start(self) -> None:
            self._running = True

        def cancel(self) -> None:
            self._running = False

        def is_running(self) -> bool:
            return self._running

        def restart(self, *a, **kw) -> None:
            self._running = True

        def before_loop(self, f):
            self._before = f
            return f

        def error(self, f):
            """Register the loop's exception handler. Real discord.py stops a
            task loop for good on an unhandled exception; a cog that omits
            this handler has a dead loop and no way to know."""
            self._error = f
            return f

    def loop(**kw):
        def deco(coro):
            return _Loop(coro, **kw)
        return deco

    tasks_mod.loop = loop

    ext.commands = commands_mod
    ext.tasks = tasks_mod

    import sys
    sys.modules["discord"] = discord
    sys.modules["discord.ui"] = ui
    sys.modules["discord.app_commands"] = app_commands
    sys.modules["discord.ext"] = ext
    sys.modules["discord.ext.commands"] = commands_mod
    sys.modules["discord.ext.tasks"] = tasks_mod


async def _async_noop_list(*_a, **_kw):
    return []
