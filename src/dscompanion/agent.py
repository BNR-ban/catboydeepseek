"""The cowork loop: ask the model, run whatever tools it asks for, repeat.

This is the piece that makes the companion able to *do* things instead of only
talking; it is the same shape as any coding agent's loop:

    stream a completion with tools
      ├─ the model asked for tool calls → (maybe ask the user) → run them →
      │   feed the results back → loop
      └─ the model answered in words → done

The whole thing runs on a worker thread and talks to the UI only through Qt
signals, so the window never blocks.  Approvals work the other way round: the
worker emits `approval_needed` and waits on a threading.Event that the UI sets.

Qt is imported here only for QThread/pyqtSignal.
"""

from __future__ import annotations

import threading
import time

from PyQt5.QtCore import QThread, pyqtSignal

from .api import APIError, SummaryStripper
from .tools import TOOL_SCHEMAS, ToolBox, preview

APPROVAL_MODES = ("off", "ask", "full")

#: DeepSeek sometimes writes its internal tool markup as plain text when no
#: tools are attached.  That must never reach the user.
MARKUP_MARKERS = ("<｜", "<|", "DSML")


def markup_at(text: str) -> int:
    """Index where leaked tool-call markup starts, or -1 if there is none."""
    cut = -1
    for marker in MARKUP_MARKERS:
        index = text.find(marker)
        if index != -1 and (cut == -1 or index < cut):
            cut = index
    return cut


def has_tool_markup(text: str) -> bool:
    return markup_at(text) != -1


def strip_tool_markup(text: str) -> str:
    """Cut leaked tool-call markup out of a model answer (text untouched if none)."""
    cut = markup_at(text)
    return text if cut == -1 else text[:cut]


class AgentWorker(QThread):
    """One user turn, including any tool calls the model makes."""

    text = pyqtSignal(str)                 # assistant text as it streams
    tool_started = pyqtSignal(str)         # one-line preview of the call
    tool_finished = pyqtSignal(str, bool, str)  # preview, ok, output
    approval_needed = pyqtSignal(str, str)  # call id, preview
    succeeded = pyqtSignal(str, str)       # answer, summary
    failed = pyqtSignal(str, str)          # kind, human message

    def __init__(self, client, history: list[dict], config, parent=None):
        super().__init__(parent)
        self._client = client
        self._history = history
        self._config = config
        self._cancel = threading.Event()
        self._toolbox = ToolBox(config)
        self._approvals: dict[str, threading.Event] = {}
        self._decisions: dict[str, str] = {}

    # ---------------------------------------------------------------- control
    def cancel(self) -> None:
        self._cancel.set()
        for event in self._approvals.values():
            event.set()

    def resolve_approval(self, call_id: str, decision: str) -> None:
        """Called from the UI thread with 'allow', 'always' or 'deny'."""
        self._decisions[call_id] = decision
        event = self._approvals.get(call_id)
        if event is not None:
            event.set()

    def _approval_mode(self) -> str:
        mode = str(self._config.get("agent.mode", "ask")).lower()
        return mode if mode in APPROVAL_MODES else "ask"

    def _ask(self, call_id: str, description: str) -> str:
        """Ask the UI and wait.  Never blocks forever: an unanswered prompt
        falls through to deny after `agent.approval_timeout_s`."""
        try:
            timeout = max(5.0, float(self._config.get("agent.approval_timeout_s", 120)))
        except (TypeError, ValueError):
            timeout = 120.0
        event = threading.Event()
        self._approvals[call_id] = event
        self.approval_needed.emit(call_id, description)
        deadline = time.monotonic() + timeout
        while not event.wait(0.2):
            if self._cancel.is_set():
                self._approvals.pop(call_id, None)
                return "deny"
            if time.monotonic() > deadline:
                self._approvals.pop(call_id, None)
                self.tool_finished.emit(description, False, "no answer — skipped")
                return "deny"
        self._approvals.pop(call_id, None)
        return self._decisions.pop(call_id, "deny")

    # ------------------------------------------------------------------- loop
    def run(self) -> None:  # noqa: D102
        stripper = SummaryStripper(self._client.summary_marker())
        messages = list(self._history)
        mode = self._approval_mode()
        # tell the model what it can actually do: with tools off it must answer
        # in words instead of pretending (or leaking raw tool markup)
        if mode == "off":
            messages.append({
                "role": "system",
                "content": ("Desktop access is switched OFF right now: you have no "
                            "tools, no shell and no file access. Answer from your own "
                            "knowledge, never emit tool-call syntax, and if the task "
                            "needs the machine, say that desktop access has to be "
                            "turned on first."),
            })
        answer_parts: list[str] = []
        try:
            steps = max(1, min(25, int(self._config.get("agent.max_steps", 8))))
            tools = TOOL_SCHEMAS if mode != "off" else None
            for _step in range(steps):
                calls: list[dict] = []
                for event in self._client.stream_events(messages, self._cancel, tools):
                    if event["type"] == "text":
                        piece = stripper.feed(event["text"])
                        if piece:
                            answer_parts.append(piece)
                            self.text.emit(piece)
                    elif event["type"] == "tool_calls":
                        calls = event["calls"]
                if not calls:
                    break
                # record the model's tool_calls turn, then run each call
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": call["id"], "type": "function",
                         "function": {"name": call["name"],
                                      "arguments": call["raw_arguments"] or "{}"}}
                        for call in calls
                    ],
                })
                for call in calls:
                    description = preview(call["name"], call["arguments"])
                    if mode == "ask":
                        if self._ask(call["id"], description) == "deny":
                            self.tool_finished.emit(description, False,
                                                    f"denied: {description}")
                            messages.append({
                                "role": "tool", "tool_call_id": call["id"],
                                "content": "Denied by the user. Ask before retrying, "
                                           "or suggest a different approach.",
                            })
                            continue
                    self.tool_started.emit(description)
                    result = self._toolbox.execute(call["name"], call["arguments"])
                    self.tool_finished.emit(description, result.ok, result.summary)
                    messages.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "content": result.output,
                    })
        except APIError as exc:
            answer = "".join(answer_parts) + stripper.feed("")
            tail, summary = stripper.finish()
            if answer or tail:
                self.succeeded.emit(strip_tool_markup((answer + tail).strip()), summary)
            self.failed.emit(exc.kind, exc.user_message())
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit("unknown", f"{exc.__class__.__name__}: {exc}")
            return

        if self._cancel.is_set():
            self.failed.emit("cancelled", "Cancelled")
            return
        tail, summary = stripper.finish()
        answer = strip_tool_markup(("".join(answer_parts) + tail).strip())
        if not answer and not summary:
            summary = ""
        self.succeeded.emit(answer, summary)
