"""The tools the companion can use on your desktop.

This is what turns him from a chat toy into something that can actually do
things: run commands, read and write files, edit code.  Everything is bounded -
a timeout per command, truncated output, a workspace directory - and the whole
feature can be switched off (``[agent] mode = "off"``) or made to ask first
(``"ask"``) exactly like DeepSeek Harness's approval modes.

Nothing in this module imports Qt: it is testable on its own.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Sent to the model as OpenAI-style function definitions.
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command with bash on the user's Linux desktop and return "
                "its combined stdout/stderr plus the exit code. Use it for anything "
                "shell-shaped: inspecting the system, git, package managers, builds, "
                "running tests, starting programs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "the command to run"},
                    "cwd": {
                        "type": "string",
                        "description": "directory to run in (defaults to the workspace)",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file, optionally only a range of lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "file path (~ allowed)"},
                    "start_line": {"type": "integer", "description": "1-based first line"},
                    "max_lines": {"type": "integer", "description": "how many lines"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a text file with the given content. Parent "
                "directories are created automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "file path (~ allowed)"},
                    "content": {"type": "string", "description": "full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact string in a file. Use for small, surgical edits; "
                "the old string must appear exactly once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "file path"},
                    "old_string": {"type": "string", "description": "text to replace"},
                    "new_string": {"type": "string", "description": "replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory (files and folders).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "directory path"}},
                "required": ["path"],
            },
        },
    },
]

MAX_STEPS_HARD_LIMIT = 25


@dataclass
class ToolResult:
    ok: bool
    output: str
    summary: str  # one short line, for the bubble and the transcript


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n… [{len(text) - limit} characters skipped] …\n{text[-tail:]}"


def _expand(path: str) -> Path:
    return Path(str(path)).expanduser()


def _workspace(config) -> Path:
    raw = str(config.get("agent.workspace", "") or "").strip()
    path = _expand(raw) if raw else Path.home()
    return path if path.is_dir() else Path.home()


class ToolBox:
    """Executes tool calls for one companion instance."""

    def __init__(self, config):
        self._config = config

    def _resolve(self, path: str) -> Path:
        """Relative paths are relative to the workspace, like a shell's cwd.

        The companion lives on the desktop, not in a terminal: a model that
        writes "notes.txt" must land in the configured workspace, never in
        whatever directory the app happened to be started from.
        """
        candidate = _expand(str(path).strip() or ".")
        if not candidate.is_absolute():
            candidate = _workspace(self._config) / candidate
        return candidate

    # ------------------------------------------------------------------ limits
    @property
    def timeout(self) -> float:
        try:
            return max(1.0, float(self._config.get("agent.timeout_s", 60)))
        except (TypeError, ValueError):
            return 60.0

    @property
    def max_output(self) -> int:
        try:
            return max(200, int(self._config.get("agent.max_output_chars", 4000)))
        except (TypeError, ValueError):
            return 4000

    # -------------------------------------------------------------- execution
    def execute(self, name: str, arguments: dict) -> ToolResult:
        handler = {
            "run_command": self._run_command,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "list_dir": self._list_dir,
        }.get(name)
        if handler is None:
            return ToolResult(False, f"unknown tool: {name}", f"unknown tool {name}")
        try:
            return handler(arguments or {})
        except Exception as exc:  # a broken tool must not kill the conversation
            return ToolResult(False, f"{exc.__class__.__name__}: {exc}", f"{name} failed")

    # -------------------------------------------------------------- run_command
    def _run_command(self, args: dict) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(False, "no command given", "run_command: empty")
        cwd = _expand(args["cwd"]) if args.get("cwd") else _workspace(self._config)
        if not Path(cwd).is_dir():
            cwd = _workspace(self._config)
        try:
            process = subprocess.Popen(
                ["bash", "-lc", command],
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return ToolResult(False, f"could not start: {exc}", f"$ {command} (failed)")
        try:
            output, _ = process.communicate(timeout=self.timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            output, _ = process.communicate()
            code = -1
            output = f"{output}\n[timed out after {self.timeout:.0f}s]"
        output = (output or "").strip() or "(no output)"
        return ToolResult(
            code == 0,
            _truncate(output, self.max_output) + f"\n[exit code {code}]",
            f"$ {command[:70]}",
        )

    # ---------------------------------------------------------------- read_file
    def _read_file(self, args: dict) -> ToolResult:
        path = self._resolve(args.get("path", ""))
        if not path.is_file():
            return ToolResult(False, f"not a file: {path}", f"read {path.name} (missing)")
        start = max(1, int(args.get("start_line") or 1))
        limit = int(args.get("max_lines") or 400)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult(False, f"could not read: {exc}", f"read {path.name} (failed)")
        chunk = lines[start - 1: start - 1 + max(1, limit)]
        numbered = "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(chunk))
        body = _truncate(numbered, self.max_output)
        if len(lines) > start - 1 + len(chunk):
            body += f"\n… [{len(lines)} lines total]"
        return ToolResult(True, body or "(empty file)",
                          f"read {path.name} ({len(chunk)} lines)")

    # --------------------------------------------------------------- write_file
    def _write_file(self, args: dict) -> ToolResult:
        path = self._resolve(args.get("path", ""))
        content = str(args.get("content", ""))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(False, f"could not write: {exc}", f"write {path.name} (failed)")
        return ToolResult(True, f"wrote {len(content)} characters to {path}",
                          f"wrote {path.name}")

    # ---------------------------------------------------------------- edit_file
    def _edit_file(self, args: dict) -> ToolResult:
        path = self._resolve(args.get("path", ""))
        old = str(args.get("old_string", ""))
        new = str(args.get("new_string", ""))
        if not old:
            return ToolResult(False, "old_string is empty", f"edit {path.name} (failed)")
        if not path.is_file():
            return ToolResult(False, f"not a file: {path}", f"edit {path.name} (missing)")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            return ToolResult(False, "old_string not found", f"edit {path.name} (not found)")
        if count > 1:
            return ToolResult(
                False, f"old_string appears {count} times - make it unique",
                f"edit {path.name} (ambiguous)",
            )
        try:
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        except OSError as exc:
            return ToolResult(False, f"could not write: {exc}", f"edit {path.name} (failed)")
        return ToolResult(True, f"edited {path}", f"edited {path.name}")

    # ---------------------------------------------------------------- list_dir
    def _list_dir(self, args: dict) -> ToolResult:
        path = self._resolve(args.get("path", "") or ".")
        if not path.is_dir():
            return ToolResult(False, f"not a directory: {path}", f"list {path} (missing)")
        entries = []
        try:
            for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
                if entry.name.startswith("."):
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"{'dir ' if entry.is_dir() else 'file'}  {entry.name}"
                               + ("" if entry.is_dir() else f"  ({size} B)"))
        except OSError as exc:
            return ToolResult(False, f"could not list: {exc}", f"list {path} (failed)")
        return ToolResult(True, _truncate("\n".join(entries) or "(empty)",
                                          self.max_output),
                          f"listed {path} ({len(entries)} entries)")


def preview(name: str, arguments: dict) -> str:
    """Short human-readable description of a pending tool call."""
    if name == "run_command":
        return f"$ {str(arguments.get('command', ''))[:120]}"
    if name == "read_file":
        return f"read {arguments.get('path', '?')}"
    if name == "write_file":
        size = len(str(arguments.get("content", "")))
        return f"write {arguments.get('path', '?')} ({size} chars)"
    if name == "edit_file":
        return f"edit {arguments.get('path', '?')}"
    if name == "list_dir":
        return f"list {arguments.get('path', '?')}"
    return name
