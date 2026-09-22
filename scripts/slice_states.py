#!/usr/bin/env python3
"""Regenerate assets/character/*.png from the supplied character sheet.

    python3 scripts/slice_states.py            # write assets/character/
    python3 scripts/slice_states.py --debug    # + removed-pixel overlays and a
                                               #   contact sheet for review

The extraction algorithm itself lives in src/dscompanion/sheet.py (it is also
what the app's optional "sheet" asset mode uses), so there is exactly one
implementation to maintain.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dscompanion.sheet import STATE_BOXES, write_assets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", default=str(ROOT / "finalcatboydeepseek.png"),
                        help="character sheet to slice (defaults to the current one; "
                             "deepseekcatboy.png and deepseekcatboyupdate.png are "
                             "still supported)")
    parser.add_argument("--out", default=str(ROOT / "assets" / "character"))
    parser.add_argument("--content-height", type=int, default=520,
                        help="height in px of the tallest pose on the shared canvas")
    parser.add_argument("--margin", type=int, default=6)
    parser.add_argument("--debug", action="store_true",
                        help="also write _debug_*.png and _contact_sheet.png")
    args = parser.parse_args()

    sheet = Path(args.sheet)
    if not sheet.exists():
        print(f"error: sheet not found: {sheet}", file=sys.stderr)
        return 1

    manifest = write_assets(sheet, args.out, args.content_height, args.margin, args.debug)
    for state in STATE_BOXES:
        info = manifest["states"][state]
        print(f"  {state:16s} canvas {info['canvas'][0]}x{info['canvas'][1]}")
    print(f"wrote {len(manifest['states'])} sprites + manifest.json to {args.out}")
    if args.debug:
        print(f"debug overlays in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
