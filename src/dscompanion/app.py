"""The controller: wires the state machine, the API, and the two windows.

Threading model
---------------
One worker thread per request owns the HTTP stream.  It reports back through Qt
signals only:

    submit → THINKING → (first token) → TALKING → (last token) → PROUD → FINISHED
                     \\→ (no token for thinking_longer_ms) → THINKING_LONGER
    any failure → LISTENING + a short error notice in the panel

Nothing polls.  Idle timers: none.  The only recurring work in the whole app is
an optional low-frequency /proc peek that is off by default.
"""

from __future__ import annotations

import os
import random
import signal
from pathlib import Path

import fcntl

from PyQt5.QtCore import QObject, QSocketNotifier, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import QAction, QActionGroup, QApplication, QMenu

from . import autostart, x11
from .agent import AgentWorker, has_tool_markup, strip_tool_markup
from .api import APIError, ChatMessage, DeepSeekClient, first_sentence
from .assets import AssetError, load_assets
from .bubble import SpeechBubble
from .character import CharacterWindow
from .chat import ChatPanel
from .config import (
    Config,
    ENV_API_KEY,
    ensure_user_config,
    write_api_key_file,
)
from .hotkey import HotkeyManager
from .states import State, StateMachine


def configure_qt_environment() -> None:
    """Keep Qt from loading a software GL stack the companion never uses.

    On a machine without a GPU driver, Qt's xcb plugin still initialises GLX and
    pulls in Mesa + LLVM - around 50 MB resident for nothing, since everything
    here is painted with the raster engine.  Must run before QApplication.
    """
    os.environ.setdefault("QT_XCB_GL_INTEGRATION", "none")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")


class SignalBridge(QObject):
    """Deliver POSIX signals through the Qt event loop, reliably.

    A Python-level `signal.signal` handler only runs when the interpreter next
    executes bytecode, and a Qt application parked inside its C++ event loop may
    not do that for minutes - which is why `kill <pid>` looked ignored.  The fix
    is `signal.set_wakeup_fd`: the *C* handler writes the signal number into a
    pipe, and a QSocketNotifier turns that byte back into a Qt signal.  Nothing
    here polls; the kernel does the waking.
    """

    received = pyqtSignal(int)

    def __init__(self, signals: tuple[int, ...], parent: QObject | None = None):
        super().__init__(parent)
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        os.set_blocking(self._write_fd, False)
        self._previous_wakeup_fd = signal.set_wakeup_fd(self._write_fd)
        for signum in signals:
            signal.signal(signum, self._noop)
        self._notifier = QSocketNotifier(self._read_fd, QSocketNotifier.Read, self)
        self._notifier.activated.connect(self._on_readable)

    @staticmethod
    def _noop(_signum, _frame) -> None:
        """The byte is written by the C-level handler, not from here."""

    def _on_readable(self) -> None:
        try:
            data = os.read(self._read_fd, 64)
        except (BlockingIOError, OSError):
            return
        for byte in data:
            self.received.emit(byte)

    def close(self) -> None:
        self._notifier.setEnabled(False)
        try:
            signal.set_wakeup_fd(self._previous_wakeup_fd)
        except (ValueError, OSError):
            pass
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def instance_path() -> Path:
    """Where the running companion advertises its pid."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / f"deepseek-companion-{os.getuid()}.pid"


_instance_fd: int | None = None


def acquire_instance_lock() -> bool:
    """True when this process is the one companion; False if one already runs."""
    global _instance_fd
    path = instance_path()
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _instance_fd = fd
    return True


def signal_running_instance() -> bool:
    """Ask an existing companion to toggle its UI."""
    try:
        pid = int(instance_path().read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        return False
    return True


class ModelsWorker(QThread):
    """Fetches the model list for the configured key, off the UI thread."""

    fetched = pyqtSignal(list)
    failed = pyqtSignal(str, str)

    def __init__(self, client: DeepSeekClient, parent=None):
        super().__init__(parent)
        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            self.fetched.emit(self._client.list_models())
        except APIError as exc:
            self.failed.emit(exc.kind, exc.user_message())
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit("unknown", f"{exc.__class__.__name__}: {exc}")


class Companion(QObject):
    """Application controller."""

    def __init__(self, app: QApplication, config: Config, assets_dir: Path,
                 start_hidden: bool = False):
        super().__init__()
        self.app = app
        self.config = config
        self.assets_dir = assets_dir
        # runtime-only: --start-hidden must not stick in the config file
        self.start_hidden = start_hidden

        self.history: list[ChatMessage] = []
        self.worker: AgentWorker | None = None
        self.last_answer = ""
        self.last_summary = ""
        self._markup_seen = False
        self._answer_parts: list[str] = []
        self.gaming = bool(config.get("gaming.enabled", False))
        self._normal_click_through = bool(config.get("character.click_through", False))
        self._process_timer: QTimer | None = None

        self.assets = load_assets(assets_dir)
        self.machine = StateMachine(config, self)
        self.client = DeepSeekClient(config)
        self.character = CharacterWindow(self.assets, config)
        # baseline for "he moved, bring the box along" (set before any signal)
        self._last_character_pos = self.character.pos()
        self.chat = ChatPanel(config)
        self.bubble = SpeechBubble(config)
        self.hotkeys = HotkeyManager(self)

        self._connect()
        self._apply_initial_state()

    # ------------------------------------------------------------------ wiring
    def _connect(self) -> None:
        self.machine.changed.connect(self._on_state_changed)
        self.character.clicked.connect(self.toggle_ui)
        self.character.context_menu_requested.connect(self._show_menu)
        self.character.geometry_changed.connect(self._on_character_moved)
        self.character.geometry_changed.connect(self._save_config_later)
        self.character.geometry_changed.connect(
            lambda: self.bubble.reposition(
                self.character.frameGeometry(),
                self.chat.frameGeometry() if self.chat.isVisible() else None,
            )
        )
        self.chat.submitted.connect(self.submit)
        self.chat.cancel_requested.connect(self.cancel)
        self.chat.clear_requested.connect(self.clear_conversation)
        self.chat.hidden_by_user.connect(self._on_chat_hidden)
        self.chat.user_activity.connect(self._on_user_typing)
        self.chat.api_key_entered.connect(self.set_api_key)
        self.chat.model_changed.connect(self.set_model)
        self.chat.attachments_changed.connect(self._on_attachments_changed)
        self.chat.models_refresh_requested.connect(self.refresh_models)
        self.chat.access_changed.connect(self.set_access_mode)
        self.chat.approval_decision.connect(self._on_approval_decision)
        self.character.petted.connect(self._on_petted)
        self.character.files_dropped.connect(self._on_files_dropped)
        self.character.pet_progress.connect(self._on_pet_progress)

        bindings = {}
        if self.config.get("hotkeys.enabled", True):
            bindings = {
                "toggle_ui": str(self.config.get("hotkeys.toggle_ui", "")),
                "toggle_click_through": str(
                    self.config.get("hotkeys.toggle_click_through", "")
                ),
            }
        if x11.is_x11():
            if not self.hotkeys.register(bindings):
                print(f"[hotkeys] unavailable: {self.hotkeys.error}")
            else:
                self.hotkeys.triggered.connect(self._on_hotkey)
                if self.hotkeys.error:
                    print(f"[hotkeys] partial: {self.hotkeys.error}")
        else:
            print(
                "[hotkeys] global hotkeys need X11; on Wayland use your compositor's "
                "own shortcut to run 'run.sh --toggle'"
            )

        self.app.aboutToQuit.connect(self.shutdown)

    def _apply_initial_state(self) -> None:
        show_character = bool(self.config.get("character.visible", True))
        start_hidden = self.start_hidden or bool(self.config.get("app.start_hidden", False))
        if show_character and not start_hidden:
            self.character.show()
        self.chat.set_models(
            self.config.get("api.known_models", []) or [],
            str(self.config.get("api.model", "")),
        )
        self.chat.set_access_mode(str(self.config.get("agent.mode", "ask")))
        if self.client.has_key():
            # ask the account which models it really has, quietly, at startup
            QTimer.singleShot(600, self.refresh_models)
        if self.config.get("app.chat_visible_on_start", False) and not start_hidden:
            self.chat.move_near(self.character.frameGeometry())
            self.chat.reveal()
        if not self.client.has_key():
            self.chat.set_status(
                f"no API key yet — paste it below, or set ${ENV_API_KEY}", error=True
            )
            self.chat.show_key_row(focus=False)
            self.chat.move_near(self.character.frameGeometry())
            if not start_hidden:
                self.chat.reveal()
        if self.gaming:
            self.set_gaming(True)
        if self.config.get("integration.watch_processes", False):
            self._start_process_watch()

    # ------------------------------------------------------------------ states
    def _on_state_changed(self, state: State, previous: State) -> None:
        self.character.set_state(state)
        self._refresh_status(state)

    def _refresh_status(self, state: State | None = None) -> None:
        if self.chat.status_is_error:
            return  # let an error notice live out its few seconds
        state = state or self.machine.state
        model = self.client.model
        mode = str(self.config.get("prompt.mode", "SHORT")).upper()
        hint = ""
        if self._detected_processes:
            hint = "  ·  " + ", ".join(self._detected_processes) + " running"
        self.chat.set_status(f"{state.label}  ·  {model}  ·  {mode}{hint}")

    # ------------------------------------------------------------------ submit
    def submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        limit = int(self.config.get("api.max_input_chars", 6000))
        if len(text) > limit:
            self.chat.set_status(f"Input too long ({len(text)} > {limit} chars)", error=True)
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if not self.client.has_key():
            self.chat.set_status("No API key — paste it below to start", error=True)
            self.machine.failed()
            self.chat.reveal()
            self.chat.show_key_row()
            return

        self.chat.set_status("")  # drop any stale error notice
        attachments = self.chat.take_attachments() if self.chat_enabled() else []
        self.history.append(ChatMessage("user", text))
        self._trim_history()
        messages = self.client.build_messages(self.history)
        if attachments:
            messages[-1] = self._with_attachments(messages[-1], attachments)

        self.last_answer = ""
        self.last_summary = ""
        self._markup_seen = False
        self._answer_parts = []
        self._markup_seen = False
        self.bubble.dismiss()
        self.chat.hide_approval()
        self.chat.begin_answer()
        self.chat.set_busy(True)
        self.machine.user_submitted()
        if self.config.get("agent.mode", "ask") != "off":
            self.chat.set_status(f"agent: {self.config.get('agent.mode')} · "
                                 f"{self.client.model}")

        worker = AgentWorker(self.client, messages, self.config, self)
        worker.text.connect(self._on_chunk)
        worker.text.connect(lambda _t: self.machine.first_token())
        worker.tool_started.connect(self._on_tool_started)
        worker.tool_finished.connect(self._on_tool_finished)
        worker.approval_needed.connect(self._on_approval_needed)
        worker.succeeded.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        worker.finished.connect(self._on_worker_finished)
        self.worker = worker
        worker.start()

    # ------------------------------------------------------------- attachments
    def _with_attachments(self, message: dict, paths: list) -> dict:
        """Attach files to the outgoing user message.

        Images ride along as real vision input (these models accept image parts),
        text files are inlined when they are small, and everything is listed by
        path so the tools can still read or edit it.
        """
        import base64
        import mimetypes

        notes: list[str] = []
        image_parts: list[dict] = []
        for raw in paths:
            path = Path(str(raw)).expanduser()
            try:
                size = path.stat().st_size
            except OSError:
                notes.append(f"- {path} (missing)")
                continue
            kind = mimetypes.guess_type(path.name)[0] or "unknown"
            notes.append(f"- {path} ({kind}, {size} bytes)")
            if kind.startswith("image/") and size <= 8_000_000:
                try:
                    encoded = base64.b64encode(path.read_bytes()).decode()
                except OSError:
                    continue
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{kind};base64,{encoded}"},
                })
            elif kind.startswith("text/") or path.suffix in (
                ".py", ".sh", ".toml", ".json", ".md", ".txt", ".yaml", ".yml", ".ini",
                ".cfg", ".c", ".h", ".cpp", ".rs", ".go", ".js", ".ts", ".css", ".html",
            ):
                if size <= 20_000:
                    try:
                        body = path.read_text(encoding="utf-8", errors="replace")
                        notes.append(f"```{path.name}\n{body[:4000]}\n```")
                    except OSError:
                        pass

        header = "Attached files:\n" + "\n".join(notes)
        text = str(message.get("content", ""))
        combined = f"{text}\n\n{header}" if text else header
        if image_parts:
            return {"role": "user", "content": [{"type": "text", "text": combined}]
                    + image_parts}
        return {"role": "user", "content": combined}

    def _pet_lines(self) -> list[str]:
        lines = self.config.get("pet.lines", []) or []
        if not isinstance(lines, (list, tuple)) or not lines:
            return ["purrr~ ♡"]
        return [str(line) for line in lines]

    def _next_pet_line(self) -> str:
        """Draw pet lines from a shuffled bag so they never repeat back to back."""
        lines = self._pet_lines()
        bag = getattr(self, "_pet_bag", None)
        if not bag:
            bag = lines[:]
            random.shuffle(bag)
            if len(bag) > 1 and bag[-1] == getattr(self, "_pet_last", None):
                bag[0], bag[-1] = bag[-1], bag[0]
            self._pet_bag = bag
        line = bag.pop()
        self._pet_last = line
        return line

    def _show_bubble(self, text: str, duration_ms: int) -> None:
        self.bubble.show_message(
            text,
            self.character.frameGeometry(),
            self.chat.frameGeometry() if self.chat.isVisible() else None,
            duration_ms=duration_ms,
        )

    def _on_character_moved(self) -> None:
        """Keep the little box with him: follow, or shift by the same delta."""
        position = self.character.pos()
        previous = getattr(self, "_last_character_pos", None)
        self._last_character_pos = position
        if previous is None or not self.chat.isVisible():
            return
        delta = position - previous
        if self.chat.pinned:
            self.chat.move_by(delta)
        else:
            self.chat.move_near(self.character.frameGeometry())

    def _on_files_dropped(self, paths: list) -> None:
        """A file landed on him: open the box and attach it."""
        if not self.chat_enabled():
            return
        if not self.character.isVisible():
            self.character.show()
        self.chat.move_near(self.character.frameGeometry())
        self.chat.reveal(focus=False)
        self.chat.attach_paths(paths)
        self._show_bubble(f"📎 {len(paths)} file(s) attached", 2200)

    def _on_petted(self) -> None:
        """A tap: one purr line in the bubble (text only, no sound)."""
        if not self.config.get("pet.enabled", True):
            return
        self._show_bubble(self._next_pet_line(),
                          int(self.config.get("pet.bubble_ms", 2600)))

    def _on_pet_progress(self, ticks: int) -> None:
        """He is being held: the purr keeps going and grows, never stopping."""
        if not self.config.get("pet.enabled", True):
            return
        ladder = self.config.get("pet.purr_ladder", []) or ["purrr~ ♡"]
        if not isinstance(ladder, (list, tuple)) or not ladder:
            ladder = ["purrr~ ♡"]
        index = min(int(ticks), len(ladder) - 1)
        tick_ms = int(self.config.get("pet.hold_tick_ms", 480))
        # refreshed every tick, so the bubble stays up as long as he is petted
        self._show_bubble(str(ladder[index]), tick_ms + 600)

    def cancel(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.chat.set_status("Stopping…")
            self.worker.cancel()

    def _on_first_token(self) -> None:
        self.machine.first_token()

    def _on_chunk(self, piece: str) -> None:
        """Stream to the panel.

        The agent worker already removed the summary line and any leaked tool
        markup before emitting, so this only has to guard against markup that
        arrives mid-stream and then show the text.
        """
        visible = piece
        if not visible:
            return
        if self._markup_seen:
            return
        if has_tool_markup(visible):
            self._markup_seen = True  # the rest of this stream is tool markup
            visible = strip_tool_markup(visible)
        if visible:
            self._answer_parts.append(visible)
            self.chat.append_token(visible)

    # ------------------------------------------------------------------- tools
    def _on_tool_started(self, description: str) -> None:
        self.chat.note_tool(description)
        self._show_bubble(f"❯ {description[:80]}", 2500)

    def _on_tool_finished(self, description: str, ok: bool, summary: str) -> None:
        self.chat.note_tool(("✓ " if ok else "✗ ") + summary)

    def _on_approval_needed(self, call_id: str, description: str) -> None:
        if not self.character.isVisible():
            self.character.show()
        self.chat.move_near(self.character.frameGeometry())
        self.chat.reveal()
        self.chat.ask_approval(call_id, description)

    def _on_approval_decision(self, call_id: str, decision: str) -> None:
        worker = self.worker
        if worker is None:
            return
        if decision == "always":
            self.set_access_mode("full")
            decision = "allow"
        worker.resolve_approval(call_id, decision)

    def set_access_mode(self, mode: str) -> None:
        mode = mode if mode in ("off", "ask", "full") else "ask"
        self.config.set("agent.mode", mode)
        self.chat.set_access_mode(mode)
        self._save_config_later()
        label = {"off": "chat only", "ask": "asks before acting",
                 "full": "full desktop access"}[mode]
        self.chat.set_status(f"Desktop access: {mode} ({label})")
        self._refresh_status(self.machine.state)

    def _on_success(self, answer: str, summary: str = "") -> None:
        answer = (answer or "").strip()
        if not summary:
            summary = first_sentence(answer)
        self.last_answer = answer
        self.last_summary = summary
        if answer:
            self.history.append(ChatMessage("assistant", answer))
            self._trim_history()
        else:
            self.chat.set_answer("(empty answer)")
        self.chat.end_answer()
        self.machine.completed()
        if summary:
            # one tidy line in the bubble, however long the model rambled
            line = " ".join(summary.split())
            if len(line) > 110:
                line = line[:107].rstrip() + "…"
            self._show_bubble(line, int(self.config.get("bubble.duration_ms", 7000)))

    def set_api_key(self, key: str) -> None:
        """Store a pasted key in the 0600 file and start using it."""
        key = key.strip()
        if not key:
            return
        try:
            # honour [api] api_key_file, so a relocated key file keeps working
            target = str(self.config.get("api.api_key_file", "") or "") or None
            path = write_api_key_file(key, target)
        except OSError as exc:
            self.chat.set_status(f"Could not save the key: {exc}", error=True)
            return
        self.chat.hide_key_row()
        self.chat.set_status("API key saved — checking which models you have…")
        print(f"[api] key stored in {path} (mode 600)")
        # knowing the real model list is what makes "it just works" possible
        self.refresh_models()

    def _on_attachments_changed(self, paths: list) -> None:
        if paths:
            self.chat.set_status(f"{len(paths)} file(s) attached to your next message")
        else:
            self._refresh_status(self.machine.state)

    def set_model(self, name: str) -> None:
        name = (name or "").strip()
        if not name or name == self.config.get("api.model"):
            return
        self.config.set("api.model", name)
        self._save_config_later()
        self.chat.set_status(f"Model: {name}")
        self._refresh_status(State.LISTENING)

    def refresh_models(self) -> None:
        """Ask DeepSeek which models this key may use."""
        if not self.client.has_key():
            self.chat.set_status("No API key yet — paste it below first", error=True)
            return
        if getattr(self, "_models_worker", None) is not None \
                and self._models_worker.isRunning():
            return
        self.chat.set_status("Fetching model list…")
        worker = ModelsWorker(self.client, self)
        worker.fetched.connect(self._on_models)
        worker.failed.connect(self._on_models_failed)
        worker.finished.connect(lambda: setattr(self, "_models_worker", None))
        self._models_worker = worker
        worker.start()

    def _all_models(self, fetched: list[str]) -> list[str]:
        """Fetched models first, then the aliases the API also accepts."""
        aliases = [str(m) for m in self.config.get("api.model_aliases", []) or []]
        seen: set[str] = set()
        merged: list[str] = []
        for name in list(fetched) + aliases:
            name = str(name).strip()
            if name and name not in seen:
                seen.add(name)
                merged.append(name)
        return merged

    def _on_models(self, models: list) -> None:
        models = self._all_models([str(m) for m in models])
        current = str(self.config.get("api.model", ""))
        self.config.set("api.known_models", models)
        notice = getattr(self, "_pending_model_notice", "")
        if current not in models and models:
            # the saved model does not exist on this account: move to one that
            # does instead of failing on the next question
            replacement = models[0]
            self.config.set("api.model", replacement)
            notice = notice or f"'{current}' is not available — switched to {replacement}"
            current = replacement
        self.chat.set_models(models, current)
        self._save_config_later()
        if notice:
            self.chat.set_status(f"{notice} · ask again", error=True)
            self._pending_model_notice = ""
        elif self.chat.status.text().startswith("Fetching"):
            self.chat.set_status(f"{len(models)} models available · {current}")
        self._refresh_status(State.LISTENING)

    def _on_models_failed(self, kind: str, message: str) -> None:
        self.chat.set_status(message, error=True)

    def _on_failure(self, kind: str, message: str) -> None:
        self.chat.end_answer()
        if kind == "cancelled":
            self.chat.set_status("Cancelled")
        elif kind == "model" and self.config.get("api.auto_switch_model", True):
            models = [str(m) for m in self.config.get("api.known_models", [])]
            if not models:
                self.chat.set_status(
                    f"{message} — press ↻ to fetch the models your key can use",
                    error=True,
                )
                self.refresh_models()
            else:
                replacement = next(
                    (m for m in models if m != self.config.get("api.model")), models[0]
                )
                self.config.set("api.model", replacement)
                self.chat.set_models(models, replacement)
                self._pending_model_notice = (
                    f"{message} — switched to {replacement}"
                )
                self.chat.set_status(f"{self._pending_model_notice}, ask again",
                                     error=True)
        else:
            self.chat.set_status(message, error=True)
        self.machine.failed()
        # No assistant turn is ever stored for a failed request (partial streams
        # included), so a retry simply resends the question.

    def _on_worker_finished(self) -> None:
        self.chat.set_busy(False)
        self.worker = None

    def _trim_history(self) -> None:
        limit = max(2, int(self.config.get("api.history_max_messages", 12)))
        if len(self.history) > limit:
            del self.history[: len(self.history) - limit]

    def clear_conversation(self) -> None:
        self.history.clear()
        self.last_answer = ""
        self.last_summary = ""
        self._markup_seen = False
        self._answer_parts = []
        self.bubble.dismiss()
        self.chat.clear_answer()
        self.machine.user_active()
        self._refresh_status(State.LISTENING)

    def _on_user_typing(self) -> None:
        if not self.machine.busy and self.machine.state is not State.LISTENING:
            self.machine.user_active()

    # ------------------------------------------------------------------- window
    def toggle_ui(self) -> None:
        """Clicking the character (or the hotkey) shows the little input box."""
        if self.gaming:
            self.set_gaming(False)
            return
        if not self.chat_enabled():
            # character-only setup: a click still pets him, the hotkey hides him
            self.character.setVisible(not self.character.isVisible())
            return
        if self.chat.isVisible():
            self.chat.hide_panel()
        else:
            self.character.show()
            self.chat.move_near(self.character.frameGeometry())
            self.chat.reveal()
        if self.machine.state is State.FINISHED:
            self.machine.user_active()

    def toggle_hotkey(self) -> None:
        """The hotkey hides everything when anything is up, else shows it."""
        if self.gaming:
            self.set_gaming(False)
            return
        if self.character.isVisible() or self.chat.isVisible():
            self.chat.hide_panel()
            self.character.hide()
        else:
            self.character.show()
            if self.chat_enabled():
                self.chat.move_near(self.character.frameGeometry())
                self.chat.reveal()
            if self.machine.state is State.FINISHED:
                self.machine.user_active()

    def chat_enabled(self) -> bool:
        return bool(self.config.get("chat.enabled", True)) and not (
            self.gaming and self.config.get("gaming.hide_chat", True)
        )

    def _on_chat_hidden(self) -> None:
        self.character.store_position()

    def _on_hotkey(self, action: str) -> None:
        if action == "toggle_ui":
            self.toggle_hotkey()
        elif action == "toggle_click_through":
            self.set_click_through(not self.character.click_through)

    def set_click_through(self, on: bool) -> None:
        self._normal_click_through = on
        self.config.set("character.click_through", on)
        self.character.set_click_through(on)
        self._save_config_later()
        self.chat.set_status("Click-through " + ("on" if on else "off"))

    def set_gaming(self, on: bool) -> None:
        self.gaming = on
        self.config.set("gaming.enabled", on)
        self.character.set_gaming(on)
        if on:
            if self.config.get("gaming.hide_chat", True):
                self.chat.hide()
            if self.config.get("gaming.hide_character", False):
                self.character.hide()
            else:
                self.character.set_scale(
                    float(self.config.get("gaming.scale", 0.85)), emit=False
                )
                self.character.set_opacity(float(self.config.get("gaming.opacity", 0.9)))
            self.character.set_click_through(bool(self.config.get("gaming.click_through", True)))
            self.character.set_always_on_top(True)
        else:
            self.character.set_scale(float(self.config.get("character.scale", 1.0)), emit=False)
            self.character.set_opacity(float(self.config.get("character.opacity", 1.0)))
            self.character.set_click_through(self._normal_click_through)
            if self.config.get("character.visible", True):
                self.character.show()
            if self.chat_enabled():
                self.chat.move_near(self.character.frameGeometry())
                self.chat.reveal()
        self._save_config_later()

    def _save_config_later(self) -> None:
        if not hasattr(self, "_save_timer"):
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(900)
            self._save_timer.timeout.connect(lambda: self.config.save())
        self._save_timer.start()

    # -------------------------------------------------------------------- menu
    def _show_menu(self, global_pos) -> None:
        menu = QMenu()
        ask = menu.addAction("Ask DeepSeek…")
        ask.triggered.connect(lambda: self._reveal_chat())
        if self.chat.pinned:
            unpin = menu.addAction("Let the box follow him again")
            unpin.triggered.connect(lambda: (self.chat.unpin(),
                                             self.chat.move_near(self.character.frameGeometry())))
        key_action = menu.addAction("Set DeepSeek API key…")
        key_action.triggered.connect(self.prompt_api_key)

        model_menu = menu.addMenu("Model")
        known = [str(m) for m in self.config.get("api.known_models", [])] or []
        current = str(self.config.get("api.model", ""))
        if current and current not in known:
            known.insert(0, current)
        for name in known:
            action = QAction(name, model_menu, checkable=True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _checked, n=name: self.set_model(n))
            model_menu.addAction(action)
        model_menu.addSeparator()
        fetch = model_menu.addAction("Fetch models from DeepSeek (↻)")
        fetch.triggered.connect(self.refresh_models)

        access_menu = menu.addMenu("Desktop access")
        current_mode = str(self.config.get("agent.mode", "ask"))
        for mode, label in (("off", "Off — chat only"),
                            ("ask", "Ask before acting"),
                            ("full", "Full access — just do it")):
            action = QAction(label, access_menu, checkable=True)
            action.setChecked(mode == current_mode)
            action.triggered.connect(lambda _checked, m=mode: self.set_access_mode(m))
            access_menu.addAction(action)
        menu.addSeparator()

        state_menu = menu.addMenu("Character state")
        group = QActionGroup(state_menu)
        for state in State:
            action = QAction(state.label, state_menu, checkable=True)
            action.setChecked(self.machine.state is state)
            action.triggered.connect(lambda _c, s=state: self.machine.set_state(s))
            group.addAction(action)
            state_menu.addAction(action)

        bigger = menu.addAction("Bigger  (+)")
        bigger.triggered.connect(lambda: self.character.set_scale(
            float(self.config.get("character.scale", 1.0)) + 0.1))
        smaller = menu.addAction("Smaller  (−)")
        smaller.triggered.connect(lambda: self.character.set_scale(
            float(self.config.get("character.scale", 1.0)) - 0.1))

        opacity_menu = menu.addMenu("Opacity")
        for value in (1.0, 0.9, 0.75, 0.5, 0.3):
            action = QAction(f"{int(value * 100)}%", opacity_menu)
            action.triggered.connect(lambda _c, v=value: self.character.set_opacity(v))
            opacity_menu.addAction(action)

        menu.addSeparator()
        top = QAction("Always on top", menu, checkable=True)
        top.setChecked(bool(self.config.get("character.always_on_top", True)))
        top.triggered.connect(self._set_always_on_top)
        menu.addAction(top)

        click = QAction("Click-through", menu, checkable=True)
        click.setChecked(self.character.click_through)
        click.triggered.connect(self.set_click_through)
        menu.addAction(click)

        gaming = QAction("Gaming mode", menu, checkable=True)
        gaming.setChecked(self.gaming)
        gaming.triggered.connect(self.set_gaming)
        menu.addAction(gaming)

        start = QAction("Start automatically", menu, checkable=True)
        start.setChecked(autostart.is_installed())
        start.triggered.connect(self._toggle_autostart)
        menu.addAction(start)

        menu.addSeparator()
        clear = menu.addAction("Clear conversation")
        clear.triggered.connect(self.clear_conversation)
        reload_action = menu.addAction("Reload character assets")
        reload_action.triggered.connect(self.reload_assets)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.app.quit)
        menu.exec_(global_pos)

    def prompt_api_key(self) -> None:
        """Reveal the panel with the paste-a-key row focused."""
        if not self.character.isVisible():
            self.character.show()
        self.chat.move_near(self.character.frameGeometry())
        self.chat.reveal()
        self.chat.show_key_row()

    def _reveal_chat(self) -> None:
        if not self.character.isVisible():
            self.character.show()
        self.chat.move_near(self.character.frameGeometry())
        self.chat.reveal()
        if self.machine.state is State.FINISHED:
            self.machine.user_active()

    def _set_always_on_top(self, checked: bool) -> None:
        self.config.set("character.always_on_top", checked)
        self.character.set_always_on_top(checked)
        self._save_config_later()

    def _toggle_autostart(self, checked: bool) -> None:
        if checked:
            path = autostart.install()
            self.chat.set_status(f"Autostart installed ({path})")
        else:
            autostart.uninstall()
            self.chat.set_status("Autostart removed")

    def reload_assets(self) -> None:
        try:
            self.assets = load_assets(self.assets_dir)
        except AssetError as exc:
            self.chat.set_status(str(exc), error=True)
            return
        self.character.reload_assets(self.assets)
        self.chat.set_status("Character assets reloaded")

    # --------------------------------------------------------- optional watcher
    _detected_processes: tuple[str, ...] = ()

    def _start_process_watch(self) -> None:
        interval = max(5, int(self.config.get("integration.check_interval_s", 20))) * 1000
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(interval)
        self._process_timer.timeout.connect(self._check_processes)
        self._process_timer.start()
        self._check_processes()

    def _check_processes(self) -> None:
        wanted = {str(n).lower() for n in self.config.get("integration.process_names", [])}
        if not wanted:
            return
        found: list[str] = []
        try:
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry.name}/comm", "r", encoding="utf-8") as fh:
                        name = fh.read().strip().lower()
                except OSError:
                    continue
                if name in wanted and name not in found:
                    found.append(name)
        except OSError:
            return
        new = tuple(sorted(found))
        if new != self._detected_processes:
            self._detected_processes = new
            self._refresh_status()

    # ------------------------------------------------------------------- close
    def shutdown(self) -> None:
        self.hotkeys.stop()
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(1500)
        self.character.store_position()
        self.client.close()
        self.config.save()


def prepare_application(argv: list[str] | None = None) -> QApplication:
    """Create the QApplication exactly as the installed app does.

    Shared with the self test so an app built by the test has the same name,
    WM_CLASS and environment as the one launched from the desktop entry.
    """
    configure_qt_environment()
    existing = QApplication.instance()
    if existing is not None:
        existing.setApplicationName("DeepSeek")
        existing.setDesktopFileName("deepseek")
        return existing
    # argv[0] decides the X11 WM_CLASS instance name; "DeepSeek" keeps it tidy
    # and matching the StartupWMClass in the installed .desktop entry
    app = QApplication(["DeepSeek"] if not argv else ["DeepSeek"] + list(argv))
    app.setApplicationName("DeepSeek")
    app.setDesktopFileName("deepseek")
    return app


def run_app(argv: list[str] | None = None) -> int:
    """Entry point used by __main__ and by tests."""
    import argparse
    import sys

    configure_qt_environment()

    parser = argparse.ArgumentParser(prog="deepseek-companion", description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--assets", default=None, help="path to assets/character")
    parser.add_argument("--start-hidden", action="store_true",
                        help="launch with only the tray-less hotkey active (autostart)")
    parser.add_argument("--set-api-key", metavar="KEY", default=None,
                        help="store KEY in the 0600 key file and exit")
    parser.add_argument("--toggle", action="store_true",
                        help="show/hide a running companion and exit")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(f"deepseek-companion {__version__}")
        return 0

    if args.set_api_key:
        path = write_api_key(args.set_api_key)
        print(f"stored API key in {path} (mode 600)")
        return 0

    if args.toggle:
        if signal_running_instance():
            print("told the running companion to toggle")
            return 0
        print("no running companion found", file=sys.stderr)
        return 1

    if not acquire_instance_lock():
        if signal_running_instance():
            print("companion already running — toggled it instead")
            return 0
        print("companion already running (could not signal it)", file=sys.stderr)
        return 1

    config = Config.load(args.config)
    ensure_user_config(config)  # documented defaults, visible before first exit
    root = Path(__file__).resolve().parent.parent.parent
    assets_dir = Path(args.assets) if args.assets else root / "assets" / "character"

    app = prepare_application()
    app.setQuitOnLastWindowClosed(False)  # hiding every window must not exit

    try:
        companion = Companion(app, config, assets_dir, start_hidden=args.start_hidden)
    except AssetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Signals arrive through the event loop (see SignalBridge): SIGTERM/SIGINT
    # quit cleanly (saving the config), SIGUSR1 is what `run.sh --toggle` sends.
    bridge = SignalBridge(
        (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1, signal.SIGHUP), companion
    )

    def _on_signal(signum: int) -> None:
        if signum == signal.SIGUSR1:
            # `deepseek --toggle` gets him out of the way (or back), exactly
            # like the global hotkey - not just the input box
            companion.toggle_hotkey()
        elif signum == signal.SIGHUP:
            pass  # the launching terminal closed: keep the companion alive
        else:
            app.quit()

    bridge.received.connect(_on_signal)

    # Nothing is force-shown here: with --start-hidden the companion waits for
    # the hotkey (or `run.sh --toggle`) instead of popping up at login.
    return app.exec_()


def write_api_key(key: str) -> Path:
    from .config import write_api_key_file

    return write_api_key_file(key)
