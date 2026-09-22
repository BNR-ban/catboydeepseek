"""Asset loading: six transparent PNGs, or the original sheet as a fallback.

Two modes, both driven by ``assets/character/manifest.json`` so changing the
mapping never means editing code:

``files`` (default, produced by scripts/slice_states.py)
    six RGBA PNGs that already share one canvas size

``sheet``
    the original character sheet plus per-state rectangles; the app extracts
    the six poses itself (same code path as the CLI) and caches the result

If no manifest exists the loader simply looks for ``<state>.png`` next to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PyQt5.QtCore import QByteArray, QSize
from PyQt5.QtGui import QPixmap

from .config import cache_dir
from .states import State

STATE_ORDER = (
    State.LISTENING,
    State.THINKING,
    State.THINKING_LONGER,
    State.TALKING,
    State.PROUD,
    State.FINISHED,
)


class AssetError(RuntimeError):
    pass


@dataclass
class AssetSet:
    pixmaps: dict[State, QPixmap] = field(default_factory=dict)
    canvas: QSize = field(default_factory=QSize)
    source: str = "files"

    def pixmap(self, state: State) -> QPixmap:
        return self.pixmaps.get(state) or next(iter(self.pixmaps.values()))

    def __len__(self) -> int:
        return len(self.pixmaps)


def _pil_to_pixmap(image) -> QPixmap:
    """PIL -> QPixmap without depending on ImageQt's Qt bindings sniffing."""
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    pixmap = QPixmap()
    if not pixmap.loadFromData(QByteArray(buffer.getvalue()), "PNG"):
        raise AssetError("Qt could not decode a generated sprite")
    return pixmap


def _load_png(path: Path) -> QPixmap:
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        raise AssetError(f"could not load {path}")
    return pixmap


def _from_files(directory: Path, manifest: dict) -> AssetSet:
    states_meta = manifest.get("states", {})
    pixmaps: dict[State, QPixmap] = {}
    canvas = QSize()
    for state in STATE_ORDER:
        info = states_meta.get(state.value) or {}
        name = info.get("file") or f"{state.value}.png"
        path = directory / name
        if not path.exists():
            continue
        pixmap = _load_png(path)
        pixmaps[state] = pixmap
        size = QSize(pixmap.width(), pixmap.height())
        if size.width() > canvas.width() or size.height() > canvas.height():
            canvas = size
    if not pixmaps:
        raise AssetError(f"no state sprites found in {directory}")
    return AssetSet(pixmaps, canvas, "files")


def _from_sheet(directory: Path, manifest: dict, use_cache: bool = True) -> AssetSet:
    sheet_name = manifest.get("sheet") or manifest.get("generated_from")
    sheet_path = (directory / sheet_name) if sheet_name else None
    if sheet_path is None or not sheet_path.exists():
        # the sheet usually lives at the project root, one level above assets/
        candidate = directory.parent.parent / str(sheet_name or "deepseekcatboy.png")
        if candidate.exists():
            sheet_path = candidate
    if sheet_path is None or not sheet_path.exists():
        raise AssetError(f"sheet mode set but the sheet is missing: {sheet_name}")

    from . import sheet as sheet_module  # local import: only needed in this mode

    content_height = int(manifest.get("content_height", 520))
    cache = cache_dir() / "sheet"
    pixmaps: dict[State, QPixmap] = {}
    canvas = QSize()

    if use_cache and (cache / "manifest.json").exists():
        try:
            cached = json.loads((cache / "manifest.json").read_text())
            if cached.get("sheet_mtime") == sheet_path.stat().st_mtime:
                return _from_files(cache, {"states": cached.get("states", {})})
        except (OSError, ValueError):
            pass

    images = sheet_module.extract_states(sheet_path, content_height)
    cache.mkdir(parents=True, exist_ok=True)
    states_meta: dict[str, dict] = {}
    for state in STATE_ORDER:
        image = images.get(state.value)
        if image is None:
            continue
        image.save(cache / f"{state.value}.png")
        pixmap = _pil_to_pixmap(image)
        pixmaps[state] = pixmap
        canvas = QSize(max(canvas.width(), pixmap.width()), max(canvas.height(), pixmap.height()))
        states_meta[state.value] = {"file": f"{state.value}.png",
                                    "canvas": [pixmap.width(), pixmap.height()]}
    if not pixmaps:
        raise AssetError("sheet extraction produced no sprites")
    (cache / "manifest.json").write_text(json.dumps({
        "sheet_mtime": sheet_path.stat().st_mtime,
        "states": states_meta,
    }))
    return AssetSet(pixmaps, canvas, "sheet")


def load_assets(directory: str | Path) -> AssetSet:
    """Load the six state sprites from `directory`."""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError as exc:
            raise AssetError(f"manifest.json is not valid JSON: {exc}") from exc

    mode = str(manifest.get("mode", "files")).lower()
    if mode == "sheet":
        return _from_sheet(directory, manifest)

    try:
        return _from_files(directory, manifest)
    except AssetError:
        if manifest:
            raise
        raise AssetError(
            f"no character sprites in {directory}\n"
            f"generate them with:  python3 scripts/slice_states.py"
        ) from None
