"""DeepSeek API access - deliberately free of any Qt import.

The UI talks to `DeepSeekClient.stream()`, which yields text deltas as they
arrive.  Swapping models is a config change; swapping providers means writing
another class with the same one-method surface.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from .config import ENV_API_KEY

# `requests` (urllib3 + ssl + charset_normalizer) costs ~20 MB resident.  The
# companion should not pay that while it is just sitting on the desktop, so it
# is imported on the first real request instead of at module import.
_requests: Any = None


def requests_module():
    global _requests
    if _requests is None:
        import requests as _r

        _requests = _r
    return _requests

RESPONSE_MODES = {
    # mode -> (max_tokens, instruction appended to the system prompt).
    # The budget must comfortably exceed the hidden "reasoning tokens" these
    # models spend before writing anything, otherwise an answer can come back
    # empty: a trivial question already burns ~100 of them.
    "SHORT": (
        900,
        "Answer in at most three short sentences. No preamble, no restating the "
        "question. Give the command or the fix first; add detail only if asked.",
    ),
    "NORMAL": (
        1800,
        "Answer concisely in a few short paragraphs or a compact list. Prefer the "
        "direct answer over background.",
    ),
    "DETAILED": (
        3600,
        "Give a thorough answer with reasoning and examples, but stay organised and "
        "skip filler.",
    ),
}


class APIError(Exception):
    """Any failure that the UI should surface, with a machine-readable kind."""

    def __init__(self, kind: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status

    def user_message(self) -> str:
        prefix = {
            "missing_key": "No API key",
            "model": "Unknown model",
            "auth": "API key rejected",
            "rate_limit": "Rate limited",
            "balance": "Insufficient balance",
            "server": "DeepSeek server error",
            "network": "Network error",
            "timeout": "Timed out",
            "bad_response": "Unreadable response",
            "bad_request": "Request rejected",
            "cancelled": "Cancelled",
        }.get(self.kind, "Request failed")
        return f"{prefix}: {self.message}"


class SummaryStripper:
    """Keeps the trailing summary line out of the visible answer.

    The model is asked to end with ``MEOW: …`` for the speech bubble.  While
    tokens stream, a tail that could still grow into that marker is held back,
    so the marker never flashes into the chat panel.
    """

    def __init__(self, marker: str = "MEOW:"):
        self.marker = marker
        self._buffer = ""

    def _marker_at(self) -> int | None:
        """Where the summary starts, if the buffer already holds the marker.

        The marker is looked for anywhere, not only at a line start: a model may
        inline it, and a chunk boundary can hide the newline.
        """
        index = self._buffer.lower().find(self.marker.lower())
        return index if index != -1 else None

    def feed(self, text: str) -> str:
        """Return the part of the stream that is safe to display right now."""
        self._buffer += text
        index = self._marker_at()
        if index is not None:
            emit, self._buffer = self._buffer[:index], self._buffer[index:]
            return emit

        target = self.marker
        limit = min(len(self._buffer), len(target))
        for size in range(limit, 0, -1):
            if target[:size].lower() == self._buffer[-size:].lower():
                emit, self._buffer = self._buffer[:-size], self._buffer[-size:]
                return emit
        emit, self._buffer = self._buffer, ""
        return emit

    def finish(self) -> tuple[str, str]:
        """Return (remaining answer text, summary or '') once the stream ends."""
        answer, summary = extract_summary(self._buffer, self.marker)
        marker_seen = summary and not answer
        return answer, summary if marker_seen else summary


def extract_summary(text: str, marker: str = "MEOW:") -> tuple[str, str]:
    """Split a reply into (visible answer, one-line summary).

    The marker may sit on its own line or inline; the answer is everything
    before its first occurrence and the summary everything after its last, so a
    model that repeats the protocol cannot leak into the answer.  Returns an
    empty summary when the marker is missing - the caller then falls back to
    `first_sentence()`.
    """
    low = text.lower()
    key = marker.lower()
    first = low.find(key)
    if first == -1:
        return text.strip(), ""
    last = low.rfind(key)
    answer = text[:first].rstrip()
    summary = text[last + len(key):].strip()
    summary = summary.splitlines()[0].strip() if summary else ""
    return answer, summary[:160]


def first_sentence(text: str, limit: int = 140) -> str:
    """A short, quotable version of an answer, for the bubble fallback."""
    flat = " ".join(text.split())
    for end in (". ", "! ", "? "):
        index = flat.find(end)
        if 0 < index < limit:
            return flat[: index + 1].strip()
    return flat[:limit].strip()


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str

    def as_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class DeepSeekClient:
    """Thin streaming client for the DeepSeek chat-completions endpoint."""

    def __init__(self, config):
        self._config = config
        self._session = None

    # ------------------------------------------------------------------ setup
    @property
    def api_key(self) -> str | None:
        return self._config.api_key

    @property
    def model(self) -> str:
        return str(self._config.get("api.model", "deepseek-chat"))

    def has_key(self) -> bool:
        return bool(self.api_key)

    def list_models(self, timeout: float = 15.0) -> list[str]:
        """Ask DeepSeek which models this key may use (GET /models)."""
        key = self.api_key
        if not key:
            raise APIError("missing_key", f"set {ENV_API_KEY} or paste the key in the app")
        requests = requests_module()
        base = str(self._config.get("api.base_url", "https://api.deepseek.com")).rstrip("/")
        try:
            response = self._get_session().get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=(float(self._config.get("api.connect_timeout_s", 10.0)), timeout),
            )
        except requests.exceptions.Timeout as exc:
            raise APIError("timeout", f"no answer from {base} ({exc})") from exc
        except requests.exceptions.RequestException as exc:
            raise APIError("network", str(exc) or exc.__class__.__name__) from exc
        if response.status_code != 200:
            raise self._http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise APIError("bad_response", "the model list was not JSON") from exc
        entries = payload.get("data") if isinstance(payload, dict) else payload
        models = []
        for entry in entries or []:
            name = entry.get("id") if isinstance(entry, dict) else entry
            if name:
                models.append(str(name))
        if not models:
            raise APIError("bad_response", "the API returned an empty model list")
        return sorted(set(models))

    def _get_session(self):
        if self._session is None:
            self._session = requests_module().Session()
        return self._session

    def close(self) -> None:
        if self._session is None:
            return
        try:
            self._session.close()
        except Exception:  # pragma: no cover - nothing useful to do
            pass

    # ----------------------------------------------------------------- prompt
    def summary_enabled(self) -> bool:
        return bool(self._config.get("prompt.summary_enabled", True))

    def summary_marker(self) -> str:
        return str(self._config.get("prompt.summary_marker", "MEOW:")).strip() or "MEOW:"

    def build_messages(self, history: Iterable[ChatMessage]) -> list[dict]:
        """system prompt + the conversation *including* the turn being asked.

        `history` already ends with the user's new message (the caller appends
        it before sending), so nothing is added here - the question must appear
        exactly once.
        """
        mode = str(self._config.get("prompt.mode", "SHORT")).upper()
        max_tokens, extra = RESPONSE_MODES.get(mode, RESPONSE_MODES["SHORT"])
        system = str(self._config.get("prompt.system_prompt", "")).strip()
        if self.summary_enabled():
            extra = f"{extra}\n\n{self._config.get('prompt.summary_instruction', '')}"
        payload = [{"role": "system", "content": f"{system}\n\n{extra}".strip()}]
        payload.extend(m.as_payload() for m in history)
        self._last_max_tokens = max_tokens
        return payload

    @staticmethod
    def mode_max_tokens(config) -> int:
        mode = str(config.get("prompt.mode", "SHORT")).upper()
        mode_tokens = RESPONSE_MODES.get(mode, RESPONSE_MODES["SHORT"])[0]
        configured = int(config.get("api.max_tokens", 0) or 0)
        return max(mode_tokens, configured)

    # ------------------------------------------------------------------ stream
    def stream(self, messages: list[dict], cancel: threading.Event) -> Iterator[str]:
        """Yield content deltas. Raises APIError on any failure."""
        key = self.api_key
        if not key:
            raise APIError(
                "missing_key",
                f"set {ENV_API_KEY} or write the key to the file named by "
                f"[api] api_key_file in the config",
            )

        base = str(self._config.get("api.base_url", "https://api.deepseek.com")).rstrip("/")
        url = f"{base}/chat/completions"
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": float(self._config.get("api.temperature", 0.7)),
            "max_tokens": int(getattr(self, "_last_max_tokens", 400)),
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeouts = (
            float(self._config.get("api.connect_timeout_s", 10.0)),
            float(self._config.get("api.read_timeout_s", 90.0)),
        )

        requests = requests_module()
        try:
            response = self._get_session().post(
                url, headers=headers, json=body, stream=True, timeout=timeouts
            )
        except requests.exceptions.Timeout as exc:
            raise APIError("timeout", f"no response from {base} ({exc})") from exc
        except requests.exceptions.SSLError as exc:
            raise APIError("network", f"TLS problem: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise APIError("network", str(exc) or exc.__class__.__name__) from exc

        with response:
            if response.status_code != 200:
                raise self._http_error(response)

            produced = False
            try:
                for raw in response.iter_lines(decode_unicode=False):
                    if cancel.is_set():
                        raise APIError("cancelled", "stopped by user")
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue  # keep-alive or comment frame
                    if isinstance(event, dict) and event.get("error"):
                        err = event["error"]
                        raise APIError(
                            "server",
                            str(err.get("message") or err) if isinstance(err, dict) else str(err),
                        )
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            produced = True
                            yield piece
            except requests.exceptions.Timeout as exc:
                raise APIError("timeout", f"stream stalled ({exc})") from exc
            except requests.exceptions.RequestException as exc:
                raise APIError("network", str(exc) or exc.__class__.__name__) from exc

            if not produced and not cancel.is_set():
                raise APIError("bad_response", "the model returned an empty answer")

    # ------------------------------------------------------- agent streaming
    def stream_events(self, messages: list[dict], cancel: threading.Event,
                      tools: list[dict] | None = None) -> Iterator[dict]:
        """Like stream(), but yields structured events, including tool calls.

        Events: {"type": "text", "text": …}
                {"type": "tool_calls", "calls": [{"id", "name", "arguments"}]}
                {"type": "done", "finish_reason": …}
        """
        key = self.api_key
        if not key:
            raise APIError("missing_key", f"set {ENV_API_KEY} or paste the key in the app")
        base = str(self._config.get("api.base_url", "https://api.deepseek.com")).rstrip("/")
        mode = str(self._config.get("prompt.mode", "SHORT")).upper()
        mode_tokens, extra = RESPONSE_MODES.get(mode, RESPONSE_MODES["SHORT"])
        configured = int(self._config.get("api.max_tokens", 0) or 0)
        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": float(self._config.get("api.temperature", 0.7)),
            "max_tokens": max(mode_tokens, configured),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        timeouts = (
            float(self._config.get("api.connect_timeout_s", 10.0)),
            float(self._config.get("api.read_timeout_s", 90.0)),
        )
        requests = requests_module()
        try:
            response = self._get_session().post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "Accept": "text/event-stream"},
                json=body, stream=True, timeout=timeouts,
            )
        except requests.exceptions.Timeout as exc:
            raise APIError("timeout", f"no response from {base} ({exc})") from exc
        except requests.exceptions.RequestException as exc:
            raise APIError("network", str(exc) or exc.__class__.__name__) from exc

        with response:
            if response.status_code != 200:
                raise self._http_error(response)
            calls: dict[int, dict] = {}
            finish = None
            produced = False
            try:
                for raw in response.iter_lines(decode_unicode=False):
                    if cancel.is_set():
                        raise APIError("cancelled", "stopped by user")
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            produced = True
                            yield {"type": "text", "text": piece}
                        for call in delta.get("tool_calls") or []:
                            produced = True
                            slot = calls.setdefault(
                                int(call.get("index", 0) or 0),
                                {"id": "", "name": "", "arguments": ""},
                            )
                            if call.get("id"):
                                slot["id"] = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                slot["name"] = function["name"]
                            if function.get("arguments"):
                                slot["arguments"] += function["arguments"]
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
            except requests.exceptions.Timeout as exc:
                raise APIError("timeout", f"stream stalled ({exc})") from exc
            except requests.exceptions.RequestException as exc:
                raise APIError("network", str(exc) or exc.__class__.__name__) from exc

            if calls:
                parsed = []
                for index in sorted(calls):
                    slot = calls[index]
                    try:
                        args = json.loads(slot["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    parsed.append({"id": slot["id"] or f"call_{index}",
                                   "name": slot["name"], "arguments": args,
                                   "raw_arguments": slot["arguments"]})
                yield {"type": "tool_calls", "calls": parsed}
            if not produced and not cancel.is_set():
                raise APIError(
                    "bad_response",
                    "the model spent its whole token budget before answering - "
                    "raise [prompt] mode or [api] max_tokens",
                )
            yield {"type": "done", "finish_reason": finish}

    # ------------------------------------------------------------------ errors
    @staticmethod
    def _http_error(response) -> APIError:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or "")
                elif err:
                    detail = str(err)
                detail = detail or str(payload.get("message") or "")
        except ValueError:
            detail = (response.text or "").strip()[:200]
        status = response.status_code
        if status == 401:
            return APIError("auth", detail or "the key was rejected", status=status)
        if status == 402:
            return APIError("balance", detail or "account balance is empty", status=status)
        if status == 429:
            return APIError("rate_limit", detail or "too many requests", status=status)
        if status in (400, 404) and "model" in detail.lower():
            return APIError("model", detail or "that model does not exist", status=status)
        if 400 <= status < 500:
            return APIError("bad_request", detail or f"HTTP {status}", status=status)
        return APIError("server", detail or f"HTTP {status}", status=status)
