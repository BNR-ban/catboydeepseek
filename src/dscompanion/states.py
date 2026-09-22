"""The six-state character state machine.

    LISTENING ──submit──▶ THINKING ──first token──▶ TALKING ──done──▶ PROUD
        ▲                    │                                        │
        │                    └─no token for thinking_longer_ms─▶ THINKING_LONGER
        │                                                             │
        └──────────────user interacts / timeout──────────────── FINISHED

Every transition is driven by a real event from the API layer (submit, first
token, completion, failure) or by the user - never by a random timer - so the
character always shows what the assistant is actually doing.

Timers are single-shot and only exist while a request is in flight or while a
short transition is pending, so an idle companion schedules nothing at all.
"""

from __future__ import annotations

from enum import Enum

from PyQt5.QtCore import QObject, QTimer, pyqtSignal


class State(str, Enum):
    LISTENING = "listening"
    THINKING = "thinking"
    THINKING_LONGER = "thinking_longer"
    TALKING = "talking"
    PROUD = "proud"
    FINISHED = "finished"

    @property
    def label(self) -> str:
        return {
            State.LISTENING: "Listening",
            State.THINKING: "Thinking",
            State.THINKING_LONGER: "Thinking (longer)",
            State.TALKING: "Talking",
            State.PROUD: "Proud",
            State.FINISHED: "Finished",
        }[self]


class StateMachine(QObject):
    """Owns the current state and all timing rules."""

    changed = pyqtSignal(object, object)  # (new State, previous State)

    def __init__(self, config, parent: QObject | None = None):
        super().__init__(parent)
        self._config = config
        self._state = State.LISTENING
        self._busy = False

        self._longer_timer = self._single_shot(self._on_thinking_too_long)
        self._proud_timer = self._single_shot(self._on_proud_hold_over)
        self._finished_timer = self._single_shot(self._on_finished_hold_over)

    # ------------------------------------------------------------------ utils
    def _single_shot(self, slot) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setTimerType(0)  # Qt.PreciseTimer not needed; coarse is cheaper
        timer.timeout.connect(slot)
        return timer

    def _ms(self, key: str, fallback: int) -> int:
        try:
            return max(0, int(self._config.get(f"behavior.{key}", fallback)))
        except (TypeError, ValueError):
            return fallback

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> State:
        return self._state

    @property
    def busy(self) -> bool:
        """True while a request is being processed."""
        return self._busy

    def _set(self, state: State) -> None:
        if state is self._state:
            return
        previous, self._state = self._state, state
        self.changed.emit(state, previous)

    def _cancel_timers(self) -> None:
        for timer in (self._longer_timer, self._proud_timer, self._finished_timer):
            if timer.isActive():
                timer.stop()

    # ---------------------------------------------------------------- events
    def user_submitted(self) -> None:
        """The user pressed Enter: a request is on its way to DeepSeek."""
        self._busy = True
        self._cancel_timers()
        self._set(State.THINKING)
        delay = self._ms("thinking_longer_ms", 2500)
        if delay:
            self._longer_timer.start(delay)

    def first_token(self) -> None:
        """Streaming really started - this is the honest moment for TALKING."""
        if not self._busy:
            return
        if self._longer_timer.isActive():
            self._longer_timer.stop()
        self._set(State.TALKING)

    def completed(self) -> None:
        """A full, successful answer arrived."""
        self._busy = False
        self._cancel_timers()
        self._set(State.PROUD)
        hold = self._ms("proud_hold_ms", 2500)
        self._proud_timer.start(max(1, hold))

    def failed(self) -> None:
        """Any API/transport failure: never stay stuck in THINKING."""
        self._busy = False
        self._cancel_timers()
        self._set(State.LISTENING)

    def cancelled(self) -> None:
        self._busy = False
        self._cancel_timers()
        self._set(State.LISTENING)

    def user_active(self) -> None:
        """The user touched the input again: back to an idle, ready pose."""
        if self._busy:
            return
        self._cancel_timers()
        self._set(State.LISTENING)

    def set_state(self, state: State) -> None:
        """Manual override (context menu / debugging)."""
        self._cancel_timers()
        self._busy = state in (State.THINKING, State.THINKING_LONGER, State.TALKING)
        self._set(state)

    # --------------------------------------------------------------- timers
    def _on_thinking_too_long(self) -> None:
        if self._busy and self._state is State.THINKING:
            self._set(State.THINKING_LONGER)

    def _on_proud_hold_over(self) -> None:
        if self._state is State.PROUD:
            self._set(State.FINISHED)
            delay = self._ms("finished_to_listening_ms", 0)
            if delay:
                self._finished_timer.start(delay)

    def _on_finished_hold_over(self) -> None:
        if self._state is State.FINISHED:
            self._set(State.LISTENING)
