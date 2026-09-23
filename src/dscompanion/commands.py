"""Slash commands: everything the menus can do, from the input box.

Type `/` in the box and the status line lists what is available, `/help` prints
the whole list, and anything unrecognised is reported instead of being sent to
the model.  Commands never reach the API and never enter the conversation
history - they are local control.

Each command is a small function returning the line to show in the status area.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .desktop import os_name
from .states import State


@dataclass
class Command:
    name: str
    args: str
    summary: str
    handler: Callable[..., str]


def _truthy(value: str | None, current: bool) -> bool:
    """on/off/yes/no/1/0, or flip when nothing is given."""
    if value is None or value == "":
        return not current
    return value.strip().lower() in ("on", "yes", "true", "1", "enable", "enabled")


def _no_args(args: list[str]) -> str | None:
    return None


# --------------------------------------------------------------------- commands
def cmd_help(app, args: list[str]) -> str:
    lines = ["Commands — type them in the box, they never reach the model:", ""]
    width = max(len(f"/{c.name} {c.args}".strip()) for c in COMMANDS)
    for command in COMMANDS:
        usage = f"/{command.name} {command.args}".strip()
        lines.append(f"  {usage:<{width}}  {command.summary}")
    lines.append("")
    lines.append("Anything not starting with / goes to DeepSeek as usual.")
    app.chat.show_help("\n".join(lines))
    return ""


def cmd_gaming(app, args: list[str]) -> str:
    on = _truthy(args[0] if args else None, app.gaming)
    app.set_gaming(on)
    return "gaming mode " + ("on — click-through, chat hidden" if on else "off")


def cmd_access(app, args: list[str]) -> str:
    mode = (args[0].lower() if args else "")
    if mode not in ("off", "ask", "full"):
        return f"access is '{app.config.get('agent.mode')}' — use: /access off|ask|full"
    app.set_access_mode(mode)
    return f"desktop access: {mode}"


def cmd_model(app, args: list[str]) -> str:
    if not args:
        app.refresh_models()
        return "fetching the model list…"
    app.set_model(args[0])
    return f"model: {args[0]}"


def cmd_models(app, args: list[str]) -> str:
    app.refresh_models()
    return "fetching the model list…"


def cmd_clear(app, args: list[str]) -> str:
    app.clear_conversation()
    return "conversation cleared"


def cmd_hide(app, args: list[str]) -> str:
    app.chat.hide_panel()
    return ""


def cmd_pet(app, args: list[str]) -> str:
    app.character.pet()
    return ""


def cmd_say(app, args: list[str]) -> str:
    text = " ".join(args).strip()
    if not text:
        return "use: /say <something cute>"
    app._show_bubble(text[:140], 4000)
    return ""


def cmd_state(app, args: list[str]) -> str:
    name = (args[0].lower() if args else "")
    names = {state.value: state for state in State}
    if name not in names:
        return "states: " + ", ".join(names)
    app.machine.set_state(names[name])
    return f"state: {name}"


def cmd_scale(app, args: list[str]) -> str:
    if not args:
        return f"scale is {app.character._scale:.2f} — use: /scale 0.5-3"
    try:
        value = float(args[0])
    except ValueError:
        return "use: /scale 0.5-3"
    app.character.set_scale(value)
    return f"scale: {app.character._scale:.2f}"


def cmd_opacity(app, args: list[str]) -> str:
    if not args:
        return f"opacity is {app.character.windowOpacity():.2f} — use: /opacity 0.2-1"
    try:
        value = float(args[0])
    except ValueError:
        return "use: /opacity 0.2-1"
    app.character.set_opacity(value)
    return f"opacity: {app.character.windowOpacity():.2f}"


def cmd_sticky(app, args: list[str]) -> str:
    current = bool(app.config.get("character.sticky", True))
    wanted = _truthy(args[0] if args else None, current)
    app.config.set("character.sticky", wanted)
    app._save_config_later()
    app.character.ensure_sticky() if wanted else _unstick(app)
    return "sticky (all desktops): " + ("on" if wanted else "off")


def _unstick(app) -> None:
    from .x11 import is_x11, set_sticky

    if is_x11():
        set_sticky(int(app.character.winId()), False)


def cmd_clickthrough(app, args: list[str]) -> str:
    on = _truthy(args[0] if args else None, app.character.click_through)
    app.set_click_through(on)
    return "click-through: " + ("on" if on else "off")


def cmd_ontop(app, args: list[str]) -> str:
    current = bool(app.config.get("character.always_on_top", True))
    on = _truthy(args[0] if args else None, current)
    app.config.set("character.always_on_top", on)
    app.character.set_always_on_top(on)
    return "always on top: " + ("on" if on else "off")


def cmd_animation(app, args: list[str]) -> str:
    current = bool(app.config.get("animation.enabled", True))
    on = _truthy(args[0] if args else None, current)
    app.config.set("animation.enabled", on)
    app.character._anim_cfg["enabled"] = on
    app.character.set_frozen(not on)
    app._save_config_later()
    return "animations: " + ("on" if on else "off (frozen)")


def cmd_key(app, args: list[str]) -> str:
    app.chat.show_key_row()
    return "paste your DeepSeek API key"


def cmd_assets(app, args: list[str]) -> str:
    app.reload_assets()
    return ""


def cmd_reset(app, args: list[str]) -> str:
    app.config.set("character.x", -1)
    app.config.set("character.y", -1)
    app.chat.unpin()
    app.character._apply_geometry()
    app.character.show()
    app.character.store_position()
    app.chat.move_near(app.character.frameGeometry())
    return "position reset to the corner"


def cmd_platform(app, args: list[str]) -> str:
    from . import desktop

    return (f"{os_name()} · {desktop.session_name()} · sticky "
            f"{'yes' if desktop.supports_sticky() else 'no'} · hotkeys "
            f"{'yes' if desktop.supports_global_hotkeys() else 'no'}")


def cmd_quit(app, args: list[str]) -> str:
    app.app.quit()
    return "bye bye ♡"


COMMANDS: list[Command] = [
    Command("help", "", "this list", cmd_help),
    Command("gaming", "[on|off]", "gaming mode (click-through, chat hidden)", cmd_gaming),
    Command("access", "off|ask|full", "desktop access level", cmd_access),
    Command("model", "[name]", "switch model (no name = refresh the list)", cmd_model),
    Command("models", "", "refetch the model list from DeepSeek", cmd_models),
    Command("clear", "", "forget the conversation", cmd_clear),
    Command("pet", "", "pet him", cmd_pet),
    Command("say", "<text>", "put words in his bubble", cmd_say),
    Command("state", "<name>", "force a pose (debug)", cmd_state),
    Command("scale", "<n>", "character size, 0.5 - 3", cmd_scale),
    Command("opacity", "<n>", "character opacity, 0.2 - 1", cmd_opacity),
    Command("animation", "[on|off]", "idle animation on/off", cmd_animation),
    Command("sticky", "[on|off]", "show on every desktop", cmd_sticky),
    Command("clickthrough", "[on|off]", "ignore the mouse", cmd_clickthrough),
    Command("ontop", "[on|off]", "always on top", cmd_ontop),
    Command("key", "", "paste your API key", cmd_key),
    Command("assets", "", "reload the character sprites", cmd_assets),
    Command("reset", "", "put him back in the corner", cmd_reset),
    Command("hide", "", "close this box", cmd_hide),
    Command("platform", "", "what this OS supports", cmd_platform),
    Command("quit", "", "close the companion", cmd_quit),
]

_INDEX = {command.name: command for command in COMMANDS}
_ALIASES = {
    "full": ("access", ["full"]),
    "ask": ("access", ["ask"]),
    "off": ("access", ["off"]),
    "f": ("access", ["full"]),
    "model-list": ("models", []),
    "s": ("say", []),
    "h": ("hide", []),
    "?": ("help", []),
    "exit": ("quit", []),
    "q": ("quit", []),
    "petpet": ("pet", []),
    "nya": ("pet", []),
}


def is_command(text: str) -> bool:
    return text.lstrip().startswith("/")


def matches(prefix: str) -> list[str]:
    """Command names starting with `prefix` (for the hint line)."""
    prefix = prefix.lstrip("/").lower()
    names = [c.name for c in COMMANDS if c.name.startswith(prefix)]
    names += [alias for alias in _ALIASES if alias.startswith(prefix)]
    return sorted(set(names))


def help_line() -> str:
    """One-line cheat sheet for the status area."""
    common = ["gaming", "access", "model", "clear", "pet", "help"]
    return "  ".join(f"/{name}" for name in common) + "   ( /help for all )"


def run(app, text: str) -> str:
    """Execute a slash command. Returns the status line to show ('' = silent)."""
    body = text.lstrip()[1:].strip()
    if not body:
        return help_line()
    parts = body.split()
    name, args = parts[0].lower(), parts[1:]
    command = _INDEX.get(name)
    if command is None and name in _ALIASES:
        target, injected = _ALIASES[name]
        command = _INDEX[target]
        args = injected + args
    if command is None:
        close = matches(name)[:4]
        hint = f" — did you mean {' '.join('/' + c for c in close)}?" if close else ""
        return f"unknown command /{name}{hint}  (try /help)"
    if command.name == "help" and args and args[0].lower() in _INDEX:
        target = _INDEX[args[0].lower()]
        return f"/{target.name} {target.args} — {target.summary}"
    try:
        return command.handler(app, args)
    except Exception as exc:  # a broken command must never break the companion
        return f"/{name} failed: {exc.__class__.__name__}: {exc}"
