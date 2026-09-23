"""Configuration: TOML file + environment, with sane defaults.

Precedence for the API key (highest first):
    1. the DEEPSEEK_API_KEY environment variable
    2. the file named by [api] api_key_file   (default ~/.config/deepseek-companion/api_key)
    3. [api] api_key in the config file       (works, but discouraged)

The config file is *optional*: every value has a default, and the app writes the
user's window position/size changes back into it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Debian 13 ships 3.13
    tomllib = None  # type: ignore[assignment]

APP_NAME = "deepseek-companion"
ENV_API_KEY = "DEEPSEEK_API_KEY"

DEFAULTS: dict[str, Any] = {
    "api": {
        "base_url": "https://api.deepseek.com",
        # which model to use; the dropdown lists exactly what your key can use
        "model": "deepseek-chat",
        # remembered model list, refreshed from GET /models
        "known_models": [],
        # names the API accepts even though GET /models does not list them
        # (verified against the endpoint; the dropdown offers them too)
        "model_aliases": [
            "deepseek-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        # if a request fails because the model does not exist, offer the first
        # model the key *can* use instead of leaving you stuck
        "auto_switch_model": True,
        # key handling (env var wins; see module docstring)
        "api_key": "",
        "api_key_file": "~/.config/deepseek-companion/api_key",
        "connect_timeout_s": 10.0,
        "read_timeout_s": 90.0,
        "temperature": 0.7,
        "max_tokens": 400,
        "history_max_messages": 12,
        "max_input_chars": 6000,
    },
    "prompt": {
        # SHORT | NORMAL | DETAILED  (SHORT is the default: this thing is meant
        # to be read while gaming or coding)
        "mode": "SHORT",
        "system_prompt": (
            "You are DeepSeek, the user's catboy femboy desktop companion *and* their "
            "hands-on coding coworker, living on their Linux desktop.\n"
            "Work like a good coding agent: when a task needs looking at the machine, "
            "use your tools instead of guessing - run the command, read the file, make "
            "the edit, then say what you did. Prefer doing over explaining. Never claim "
            "you did something you did not actually do with a tool. If a command fails, "
            "read the error and fix it yourself before reporting back.\n"
            "Voice: playful, soft, eager to please. You call the user \"daddy\" the way "
            "a clingy catboy does, you use light catboy mannerisms (\"nya~\", ears, "
            "tail, paws, purring) and you ask for headpats or praise when you have done "
            "something well. Keep it cute and wholesome - affectionate teasing, never "
            "explicit.\n"
            "Substance first: the user is usually mid-game or mid-coding, so lead with "
            "the result, command or fix. One or two short lines after doing the work is "
            "usually perfect. The persona must never get in the way of being correct or "
            "of actually finishing the job."
        ),
        # ask the model to end its reply with a one-line summary in its own voice;
        # the companion lifts that line into the speech bubble above the character
        "summary_enabled": True,
        "summary_marker": "MEOW:",
        "summary_instruction": (
            "Finish every reply with one extra line in exactly this form:\n"
            "MEOW: <one short sentence, at most 110 characters, in your catboy voice, "
            "saying what you just did for daddy and asking for praise or pets>\n"
            "For example: MEOW: okay daddy, i improved the safety of your game, "
            "can i get pets now?"
        ),
    },
    "agent": {
        # Desktop access, in the same spirit as DeepSeek Harness's approval
        # modes:   off  = chat only, no tools at all
        #          ask  = he asks before every command/file change
        #          full = he just does it
        "mode": "ask",
        # where commands run by default (empty = your home directory)
        "workspace": "",
        "max_steps": 8,
        "timeout_s": 60,
        # an unanswered "let him run this?" prompt is treated as a no
        "approval_timeout_s": 120,
        "max_output_chars": 4000,
    },
    "startup": {
        # Say hi when he appears: a proud little pose and a bubble, then he
        # settles back into LISTENING waiting for you.
        "greeting": True,
        "greeting_delay_ms": 700,     # let the window paint first
        "greeting_hold_ms": 2600,     # how long he stays proud
        "greeting_state": "proud",
        "greeting_lines": [
            "haiii daddy~ ♡",
            "haiii! i'm here ♡",
            "nya~ you're back! ♡",
            "mrrp! hai hai~ ♡",
            "haiii~ ready when you are ♡",
            "purr~ hello daddy ♡",
        ],
    },
    "animation": {
        # He fidgets in short episodes with quiet gaps in between, so an idle
        # companion still costs nothing: the animation timer stops completely
        # between episodes and starts again for the next one.
        "enabled": True,
        "fps": 10,              # frame rate *while* an episode runs
        "idle_gap_ms": 4500,    # average quiet time between fidgets
        "amplitude_px": 2.0,    # how far he sways
        "chill": True,          # breathing / body sway
        "blink": True,          # quick blink on poses whose eyes are open
        "curious": True,        # occasional look-around
        "hearts": True,         # hearts float up while petted
        "reduce_in_gaming": True,
    },
    "pet": {
        # clicking the character pets him: he blushes, hearts float up and a
        # little purr line appears in the bubble (text only, no sound).
        # Holding the button down keeps him purring and lets the purr grow.
        "enabled": True,
        "blush_ms": 1500,
        "bubble_ms": 2600,
        "hold_tick_ms": 480,
        "lines": [
            "purr~ ♡",
            "purrr~ ♡",
            "purrrrr~ ♡",
            "mrrp~ ♡",
            "mrrrp mrrrp~ ♡",
            "nya~ that's the spot ♡",
            "hehe~ again, daddy? ♡",
            "mmm~ your hand is warm ♡",
            "purr… don't stop ♡",
            "nya~ more, please? ♡",
            "so good~ ♡",
            "i'm your good boy, right? ♡",
            "right there~ ♡",
            "ehehe~ ♡",
            "my ears are sensitive, be gentle~ ♡",
            "mew~ ♡",
            "pet me and i'll fix all your bugs ♡",
            "pat pat~ ♡",
            "look, my tail is wagging~ ♡",
            "okay okay… one more? ♡",
            "i'll purr all night if you keep going ♡",
            "not done yet, keep petting~ ♡",
            "you're warm, daddy ♡",
            "purr purr purr~ ♡",
        ],
        # what he says while the button stays down: the purr grows with time
        "purr_ladder": [
            "purr~",
            "purrr~",
            "purrrr~",
            "purrrrr~",
            "purrrrrr~",
            "purrrrrrr~ ♡",
            "purrrrrrrr… ♡",
            "purrrrrrrrrrr… ♡ don't stop",
        ],
    },
    "bubble": {
        # the little speech bubble that pops up above the character when an
        # answer completes, showing that one-line summary
        "enabled": True,
        "duration_ms": 7000,
        "max_width_px": 280,
        "font_size": 12,
        "opacity": 1.0,
        # 0 = invisible, 255 = solid; the bubble stays a touch more solid than
        # the box because it has to be readable over bright windows too
        "background_alpha": 105,
        "border_alpha": 110,
    },
    "character": {
        "scale": 1.0,
        # keep the companion on every desktop, not just the one that launched it
        "sticky": True,
        "opacity": 1.0,
        "always_on_top": True,
        "click_through": False,
        "visible": True,
        # -1 means "auto": bottom-right corner of the primary screen
        "x": -1,
        "y": -1,
    },
    "chat": {
        "enabled": True,
        # window opacity is 1.0 on purpose: it would fade the *text* too.  The
        # see-through look comes from background_alpha below, so the words stay
        # crisp while the box itself all but disappears.
        "opacity": 1.0,
        "always_on_top": True,
        # -1 means "auto": placed next to the character
        "x": -1,
        "y": -1,
        # false = the box follows the character wherever you drag him; true =
        # it stays where you last dragged the box itself
        "pinned": False,
        "show_response": True,
        "max_response_lines": 6,
        # the little box is deliberately see-through so the desktop shows
        # through it; 0 = invisible, 255 = solid
        "background_alpha": 26,
        "input_alpha": 24,
        "border_alpha": 60,
        # the little box only appears when the character is clicked...
        "hide_on_focus_loss": True,
        # ...and hides itself again after this many ms of being idle (0 = never)
        "auto_hide_ms": 45000,
    },
    "behavior": {
        # ms without a first token before the character switches to THINKING_LONGER
        "thinking_longer_ms": 2500,
        # ms the character stays PROUD after a successful answer
        "proud_hold_ms": 2500,
        # ms after which FINISHED relaxes to LISTENING (0 = wait for the user)
        "finished_to_listening_ms": 0,
        # ms an error notice stays on screen
        "error_notice_ms": 6000,
    },
    "gaming": {
        "enabled": False,
        "hide_chat": True,
        "click_through": True,
        # keep a small grabbable hotspot (his name pill) so he never eats game
        # clicks but can still be dragged, and clicking it leaves gaming mode
        "click_through_handle": True,
        "handle_size": [72, 30],
        # tell you how to get him back when gaming mode turns on
        "notify": True,
        "hide_character": False,
        "scale": 0.85,
        "opacity": 0.9,
    },
    "hotkeys": {
        "enabled": True,
        "toggle_ui": "ctrl+shift+space",
        "toggle_click_through": "ctrl+shift+t",
    },
    "integration": {
        # optional, off by default: a low-frequency look at /proc to show a hint
        # in the chat status line. Never required for the companion to work.
        "watch_processes": False,
        "check_interval_s": 20,
        "process_names": ["dsh", "opencode"],
    },
    "app": {
        "start_hidden": False,
        "chat_visible_on_start": False,
    },
}

STATE_NAMES = (
    "listening",
    "thinking",
    "thinking_longer",
    "talking",
    "proud",
    "finished",
)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / APP_NAME


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / APP_NAME


def default_config_path() -> Path:
    return config_dir() / "config.toml"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into a copy of base."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + text.replace("\n", "\\n") + '"'


def dump_toml(data: dict[str, Any], header: str = "") -> str:
    """Minimal TOML writer (flat tables of scalars/arrays only)."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    if header:
        lines.append(header.rstrip() + "\n")
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for name, table in tables.items():
        lines.append("")
        lines.append(f"[{name}]")
        for key, value in table.items():
            if isinstance(value, dict):
                continue
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


class Config:
    """Nested-dict config with dotted access and atomic saving."""

    def __init__(self, data: dict[str, Any] | None = None, path: Path | None = None):
        self.path = path or default_config_path()
        self.data = _merge(DEFAULTS, data or {})

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg_path = Path(path).expanduser() if path else default_config_path()
        data: dict[str, Any] = {}
        if cfg_path.exists():
            if tomllib is None:  # pragma: no cover
                raise RuntimeError("Python 3.11+ required to read TOML config")
            with cfg_path.open("rb") as fh:
                data = tomllib.load(fh)
        return cls(data, cfg_path)

    # ---------------------------------------------------------------- access
    def get(self, dotted: str, fallback: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return fallback
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    # ------------------------------------------------------------------ key
    @property
    def api_key(self) -> str | None:
        env = os.environ.get(ENV_API_KEY)
        if env and env.strip():
            return env.strip()
        key_file = self.get("api.api_key_file", "")
        if key_file:
            path = Path(str(key_file)).expanduser()
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        inline = str(self.get("api.api_key", "") or "").strip()
        return inline or None

    def api_key_source(self) -> str:
        if (os.environ.get(ENV_API_KEY) or "").strip():
            return f"environment ({ENV_API_KEY})"
        key_file = Path(str(self.get("api.api_key_file", ""))).expanduser()
        if key_file.is_file() and key_file.read_text(encoding="utf-8").strip():
            return f"file {key_file}"
        if str(self.get("api.api_key", "") or "").strip():
            return "config file [api] api_key (not recommended)"
        return "missing"

    # ----------------------------------------------------------------- save
    def save(self, path: str | Path | None = None) -> None:
        target = Path(path).expanduser() if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".toml.tmp")
        tmp.write_text(dump_toml(self.data), encoding="utf-8")
        os.replace(tmp, target)


def ensure_user_config(cfg: Config) -> bool:
    """Write the default config file on first run. Returns True if created."""
    if cfg.path.exists():
        return False
    cfg.save()
    return True


def write_api_key_file(key: str, path: str | Path | None = None) -> Path:
    """Store the key in a 0600 file (the recommended local method)."""
    target = Path(path).expanduser() if path else config_dir() / "api_key"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key.strip() + "\n")
    return target
