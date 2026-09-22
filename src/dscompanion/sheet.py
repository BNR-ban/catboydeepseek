"""Character-sheet extraction.

The supplied sheet (``deepseekcatboy.png``) is a 3x2 grid of panels plus a
footer banner.  Each panel holds the *same* character in one of six poses that
map 1:1 onto the companion's states:

    panel (row 1, col 1)  THINKING          (hand on chin, "?" cloud)
    panel (row 1, col 2)  THINKING_LONGER   (slumped over the keyboard)
    panel (row 1, col 3)  PROUD             (eyes closed, sparkles)
    panel (row 2, col 1)  TALKING           (pointing, speech bubble)
    panel (row 2, col 2)  FINISHED          (hands clasped, checkmark)
    panel (row 2, col 3)  LISTENING         (hand to ear, ear icon)

``extract_states()`` turns that sheet into six transparent, same-scale sprites
that share one canvas and one baseline, so the character never jumps when the
state changes.

How the isolation works, and why it is not just "remove dark pixels":

* the panel background is removed with a flood fill seeded from the panel rim,
  so dark *interior* pixels (the black shirt, hood shadows) survive;
* flat panel-coloured pockets that the flood cannot reach - the original title
  card paints a glow frame behind the character - are removed only for panels
  listed in a profile's ``interior_clean``;
* the character is the largest connected component; overlay art (title, logo,
  bullet list), panel furniture and dark decoration fills are separate
  components and get dropped, while small *bright* props (sparkles, the "?" and
  the listening badge) are kept;
* a morphological close repairs hair/face creases whose colour is identical to
  the panel background, and the anti-aliased rim is un-premultiplied so no grey
  halo is baked into the sprite.

Nothing here imports Qt, so regenerating assets stays a headless operation.
"""

from __future__ import annotations

import json
from array import array
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter

# --------------------------------------------------------------------------
# Sheet profiles
# --------------------------------------------------------------------------
# Every sheet gets its own measured geometry.  The profile is chosen by file
# name so both sheets in this project stay usable, and unknown files fall back
# to the newest profile (they are all 3x2 grids of the same six poses).
#
# Keys:
#   boxes          (left, top, right, bottom) of each panel, in sheet pixels
#   inset          pixels trimmed from each panel edge to drop its rounded frame
#   background     the panel background colour
#   label_rects    panel-relative rects of art that is *painted over* the
#                  character (the update sheet labels each panel); they are
#                  inpainted away from the surrounding artwork
#   character_only keep the character silhouette and nothing else
#   interior_clean panels with pockets of panel colour walled off by overlay art
#   keep_rects     components to keep regardless (floating punctuation etc.)
# --------------------------------------------------------------------------
PROFILES: dict[str, dict] = {
    # original sheet: white-haired catboy
    "deepseekcatboy.png": {
        "boxes": {
            "thinking": (12, 22, 416, 515),
            "thinking_longer": (430, 22, 843, 515),
            "proud": (863, 22, 1250, 515),
            "talking": (20, 577, 443, 1008),
            "finished": (462, 577, 837, 1008),
            "listening": (854, 577, 1240, 1008),
        },
        "inset": {
            "thinking": (0, 0, 0, 0),
            "thinking_longer": (6, 6, 6, 44),
            "proud": (6, 6, 6, 6),
            "talking": (6, 6, 6, 6),
            "finished": (6, 6, 6, 6),
            "listening": (6, 6, 6, 6),
        },
        "background": (16, 20, 29),
        "label_rects": {},
        "character_only": {"thinking"},
        "interior_clean": {"thinking"},
        "keep_rects": {"thinking": [(300, 100, 404, 230)]},
    },
    # update sheet: blue-haired catboy, panels labelled and edge to edge
    "deepseekcatboyupdate.png": {
        # the panels are cut just above their state label: that label is painted
        # across the character, and no fill can invent the artwork underneath.
        # The remaining edge of the label is removed by label_rects below.
        "boxes": {
            "thinking": (21, 60, 412, 524),
            "thinking_longer": (424, 60, 838, 524),
            "proud": (855, 60, 1236, 524),
            "talking": (21, 598, 412, 960),
            "finished": (424, 598, 838, 960),
            "listening": (855, 598, 1236, 960),
        },
        "inset": {
            "thinking": (5, 5, 5, 5),
            "thinking_longer": (5, 5, 5, 5),
            "proud": (5, 5, 5, 5),
            "talking": (5, 5, 5, 5),
            "finished": (5, 5, 5, 5),
            "listening": (5, 5, 5, 5),
        },
        "background": (10, 13, 20),
        # the "Thinking" / "Proud" / … pills are painted on top of the artwork
        # only the label's top edge survives the crop above; it is inpainted from
        # the artwork just above it
        "label_rects": {
            "thinking": [(80, 505, 348, 524)],
            "thinking_longer": [(464, 499, 803, 524)],
            "proud": [(970, 501, 1186, 524)],
            "talking": [(84, 941, 356, 960)],
            "finished": [(530, 941, 776, 960)],
            "listening": [(944, 941, 1200, 960)],
        },
        "character_only": {"listening", "thinking", "thinking_longer",
                           "talking", "proud", "finished"},
        # this art is bright and saturated, its dark cloth sits well above the
        # very dark tile colour, so enclosed background pockets are safe to drop
        # - and the threshold is low because the artwork chops those pockets
        # into small pieces
        "interior_clean": {"listening", "thinking", "thinking_longer",
                           "talking", "proud", "finished"},
        "interior_min_px": 140,
        "keep_rects": {},
    },
    # final sheet: every pose is already cut out (real alpha), so nothing has to
    # be flood-filled - the six characters are simply the six biggest islands.
    # The "deepseek / your AI catboy companion" block in the top left is dropped
    # on request, together with the footer chibis.
    "finalcatboydeepseek.png": {
        "mode": "alpha",
        "boxes": {
            "thinking": (55, 116, 516, 502),
            "thinking_longer": (518, 100, 1012, 506),
            "proud": (1016, 4, 1530, 500),
            "talking": (50, 504, 516, 862),
            "finished": (520, 505, 1000, 860),
            "listening": (1016, 508, 1520, 862),
        },
        # the "deepseek / your AI catboy companion" block is erased outright on
        # request.  It ends by y=145 and the thinking pose's hair starts at 150,
        # so a clean cut is possible; its word "your" also reaches into the
        # thinking pose's box, which is why an island filter alone is not enough.
        "erase_rects": [(0, 0, 780, 148)],
        "min_island_px": 600,
        "background": (10, 13, 20),
    },
}

DEFAULT_PROFILE = "finalcatboydeepseek.png"

# Fallbacks for panels whose profile omits a key.
FILL_TOLERANCE = 22      # border-seeded flood fill, also eats the AA halo
INTERIOR_TOLERANCE = 20  # enclosed panel-colour pockets
INTERIOR_MIN_PX = 400
DECOR_MIN_PX = 24
DECOR_MIN_LUMA = 85
LINE_MAX_THICKNESS = 4
CLOSE_RADIUS = 3


def profile_for(sheet_path) -> dict:
    return PROFILES.get(Path(sheet_path).name, PROFILES[DEFAULT_PROFILE])


def _inpaint(image: "Image.Image", rects, background) -> None:
    """Paint out overlay art by continuing each column through the masked span.

    The update sheet draws its state labels across the character, so the pills
    are removed before anything else.  Each masked column is filled from the
    smoothed colours just above and below it: that keeps the local shading, and
    it keeps tile *background* looking like background so the flood fill can
    still remove it.  (A 2D diffusion fill smears bright cloth into the empty
    area and leaves a visible blob; an unsmoothed column fill leaves stripes.)
    """
    if not rects:
        return
    px = image.load()
    w, h = image.size
    mask = bytearray(w * h)
    spans: dict[int, tuple[int, int]] = {}
    for rx0, ry0, rx1, ry1 in rects:
        top, bottom = max(0, ry0), min(h, ry1)
        if top >= bottom:
            continue
        for y in range(top, bottom):
            base = y * w
            for x in range(max(0, rx0), min(w, rx1)):
                mask[base + x] = 1
        for x in range(max(0, rx0), min(w, rx1)):
            if x in spans:
                lo, hi = spans[x]
                spans[x] = (min(lo, top), max(hi, bottom))
            else:
                spans[x] = (top, bottom)

    def sample(x: int, y: int, radius: int = 4):
        """Average of unmasked pixels around (x, y) - kills column striping."""
        if y < 0 or y >= h:
            return None
        total = [0, 0, 0]
        seen = 0
        for dx in range(-radius, radius + 1):
            nx = x + dx
            if 0 <= nx < w and not mask[y * w + nx]:
                c = px[nx, y]
                total[0] += c[0]
                total[1] += c[1]
                total[2] += c[2]
                seen += 1
        if not seen:
            return None
        return (total[0] // seen, total[1] // seen, total[2] // seen)

    for x, (top, bottom) in spans.items():
        above = sample(x, top - 1)
        below = sample(x, bottom)
        if above is None and below is None:
            colour = background
            for y in range(top, bottom):
                px[x, y] = colour
            continue
        if below is None:
            below = above
        elif above is None:
            above = below
        length = bottom - top
        for index, y in enumerate(range(top, bottom)):
            t = (index + 1) / (length + 1)
            px[x, y] = tuple(
                int(round(above[c] + (below[c] - above[c]) * t)) for c in range(3)
            )


class Grid:
    """A cropped panel with a boolean background mask."""

    def __init__(self, image: Image.Image, state: str, profile: dict | None = None):
        profile = profile or PROFILES[DEFAULT_PROFILE]
        self.profile = profile
        x0, y0, x1, y1 = profile["boxes"][state]
        il, it, ir, ib = profile["inset"][state]
        self.state = state
        self.box = (x0 + il, y0 + it, x1 - ir, y1 - ib)
        self.img = image.crop(self.box)
        # overlay art (painted on top of the character) goes first
        label_rects = [
            (rx0 - x0 - il, ry0 - y0 - it, rx1 - x0 - il, ry1 - y0 - it)
            for rx0, ry0, rx1, ry1 in profile.get("label_rects", {}).get(state, [])
        ]
        _inpaint(self.img, label_rects, profile["background"])
        self.w, self.h = self.img.size
        self.px = self.img.load()
        self.bg = bytearray(self.w * self.h)
        self.panel_color = self._measure_panel_color()

    def _measure_panel_color(self) -> tuple[int, int, int]:
        """Average colour of the panel background, sampled from its rim."""
        w, h = self.w, self.h
        pts = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
        pts += [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
        tot = [0, 0, 0]
        n = 0
        for x, y in pts:
            if self._near_bg(x, y, 12):
                c = self.px[x, y]
                tot[0] += c[0]
                tot[1] += c[1]
                tot[2] += c[2]
                n += 1
        if not n:
            return self.profile["background"]
        return (tot[0] // n, tot[1] // n, tot[2] // n)

    def _near_bg(self, x: int, y: int, tol: int) -> bool:
        r, g, b = self.px[x, y]
        bg = self.profile["background"]
        return (
            abs(r - bg[0]) <= tol
            and abs(g - bg[1]) <= tol
            and abs(b - bg[2]) <= tol
        )

    def flood_background(self) -> None:
        """Mark every background pixel reachable from the panel border."""
        dq: deque[tuple[int, int]] = deque()
        w, h, bg = self.w, self.h, self.bg

        def seed(x: int, y: int) -> None:
            i = y * w + x
            if not bg[i] and self._near_bg(x, y, FILL_TOLERANCE):
                bg[i] = 1
                dq.append((x, y))

        for x in range(w):
            seed(x, 0)
            seed(x, h - 1)
        for y in range(h):
            seed(0, y)
            seed(w - 1, y)

        while dq:
            x, y = dq.popleft()
            if x and not bg[y * w + x - 1] and self._near_bg(x - 1, y, FILL_TOLERANCE):
                bg[y * w + x - 1] = 1
                dq.append((x - 1, y))
            if x + 1 < w and not bg[y * w + x + 1] and self._near_bg(x + 1, y, FILL_TOLERANCE):
                bg[y * w + x + 1] = 1
                dq.append((x + 1, y))
            if y and not bg[(y - 1) * w + x] and self._near_bg(x, y - 1, FILL_TOLERANCE):
                bg[(y - 1) * w + x] = 1
                dq.append((x, y - 1))
            if y + 1 < h and not bg[(y + 1) * w + x] and self._near_bg(x, y + 1, FILL_TOLERANCE):
                bg[(y + 1) * w + x] = 1
                dq.append((x, y + 1))

    def interior_pass(self) -> None:
        """Drop flat panel-coloured pockets walled off from the border.

        Only used for panels named in the profile's interior_clean list.
        """
        w, h, bg = self.w, self.h, self.bg
        seen = bytearray(w * h)
        for sy in range(h):
            for sx in range(w):
                i = sy * w + sx
                if bg[i] or seen[i] or not self._near_bg(sx, sy, INTERIOR_TOLERANCE):
                    continue
                dq = deque([(sx, sy)])
                seen[i] = 1
                region = [(sx, sy)]
                while dq:
                    x, y = dq.popleft()
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            nx, ny = x + dx, y + dy
                            if not (0 <= nx < w and 0 <= ny < h):
                                continue
                            j = ny * w + nx
                            if seen[j] or bg[j]:
                                continue
                            if not self._near_bg(nx, ny, INTERIOR_TOLERANCE):
                                continue
                            seen[j] = 1
                            region.append((nx, ny))
                            dq.append((nx, ny))
                if len(region) >= self.profile.get("interior_min_px", INTERIOR_MIN_PX):
                    for x, y in region:
                        bg[y * w + x] = 1

    def components(self) -> tuple[list[dict], "array"]:
        """8-connected foreground components with size, bbox and mean luma."""
        w, h, bg = self.w, self.h, self.bg
        seen = bytearray(w * h)
        labels = array("i", bytes(4 * w * h))
        out: list[dict] = []
        for sy in range(h):
            for sx in range(w):
                if bg[sy * w + sx] or seen[sy * w + sx]:
                    continue
                label = len(out)
                dq = deque([(sx, sy)])
                seen[sy * w + sx] = 1
                labels[sy * w + sx] = label + 1
                n = 0
                luma = 0
                mnx = mxx = sx
                mny = mxy = sy
                while dq:
                    x, y = dq.popleft()
                    n += 1
                    r, g, b = self.px[x, y]
                    luma += (r * 299 + g * 587 + b * 114) // 1000
                    mnx = min(mnx, x)
                    mxx = max(mxx, x)
                    mny = min(mny, y)
                    mxy = max(mxy, y)
                    for dy in (-1, 0, 1):
                        ny = y + dy
                        if not 0 <= ny < h:
                            continue
                        for dx in (-1, 0, 1):
                            nx = x + dx
                            if not 0 <= nx < w:
                                continue
                            j = ny * w + nx
                            if bg[j] or seen[j]:
                                continue
                            seen[j] = 1
                            labels[j] = label + 1
                            dq.append((nx, ny))
                out.append({
                    "size": n,
                    "bbox": (mnx, mny, mxx, mxy),
                    "luma": luma // max(1, n),
                })
        # Labels were handed out in scan order; rank them so that label 1 is
        # always the character (the biggest component) and renumber the map.
        order = sorted(range(len(out)), key=lambda i: out[i]["size"], reverse=True)
        remap = array("i", bytes(4 * (len(out) + 1)))
        for rank, old in enumerate(order, start=1):
            remap[old + 1] = rank
        out = [out[i] for i in order]
        for i in range(len(labels)):
            if labels[i]:
                labels[i] = remap[labels[i]]
        return out, labels

    def build_sprite(self) -> Image.Image:
        """Return the isolated character as RGBA with a soft, halo-free edge."""
        self.flood_background()
        if self.state in self.profile.get("interior_clean", ()): 
            self.interior_pass()
        comps, labels = self.components()
        if not comps:
            raise SystemExit(f"state {self.state!r}: panel appears to be empty")

        # Component 0 is the character.  Everything else on these panels is
        # either overlay art (title, wordmark, bullet list), panel furniture
        # (frame rules, vignette) or a dark decoration fill, and all of it is
        # measurably detached from the character, so the only extras worth
        # keeping are small *bright* props: sparkles, the ear badge, the "?".
        keep_rects = self.profile.get("keep_rects", {}).get(self.state, [])
        character_only = self.state in self.profile.get("character_only", ())
        alpha = Image.new("L", (self.w, self.h), 0)
        apx = alpha.load()
        for index, comp in enumerate(comps):
            cx0, cy0, cx1, cy1 = comp["bbox"]
            if index:
                in_rect = any(
                    rx0 <= cx0 and ry0 <= cy0 and cx1 <= rx1 and cy1 <= ry1
                    for rx0, ry0, rx1, ry1 in keep_rects
                )
                if not in_rect:
                    if character_only or comp["size"] < DECOR_MIN_PX:
                        continue
                    if comp["luma"] < DECOR_MIN_LUMA:
                        continue
                    thin = min(cx1 - cx0, cy1 - cy0) <= LINE_MAX_THICKNESS
                    long_enough = max(cx1 - cx0, cy1 - cy0) >= 40
                    if thin and long_enough:
                        continue  # stray inner frame rule
            label = index + 1
            for y in range(cy0, cy1 + 1):
                row = y * self.w
                for x in range(cx0, cx1 + 1):
                    # match on the label map: the character's bounding box also
                    # contains dropped components (overlay text, stray rules)
                    if labels[row + x] == label:
                        apx[x, y] = 255

        # The artwork casts shadows whose colour is identical to the panel
        # background, so a colour test alone saws thin cracks through the hair
        # and the face.  A morphological close reconnects anything narrower
        # than the kernel while leaving genuinely open background alone.
        if CLOSE_RADIUS:
            k = 2 * CLOSE_RADIUS + 1
            alpha = alpha.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))
        # Then trim the anti-aliased rim: erode a hair, feather, and let
        # _decontaminate() un-blend the semi-transparent edge pixels.
        alpha = alpha.filter(ImageFilter.MinFilter(3))
        alpha = alpha.filter(ImageFilter.GaussianBlur(0.7))

        sprite = self.img.convert("RGBA")
        sprite.putalpha(alpha)
        bbox = alpha.getbbox()
        self.bbox = bbox
        if bbox is None:
            raise SystemExit(f"state {self.state!r}: nothing left after background removal")
        sprite = sprite.crop(bbox)
        self._decontaminate(sprite)
        self.debug_alpha = alpha.crop(bbox)
        return sprite

    def _decontaminate(self, sprite: Image.Image) -> None:
        """Undo background blending on the semi-transparent rim, in place."""
        bg = self.panel_color
        px = sprite.load()
        w, h = sprite.size
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if a == 0 or a == 255:
                    continue
                inv = 255 - a
                px[x, y] = (
                    min(255, max(0, (r * 255 - bg[0] * inv) // a)),
                    min(255, max(0, (g * 255 - bg[1] * inv) // a)),
                    min(255, max(0, (b * 255 - bg[2] * inv) // a)),
                    a,
                )


def compose_canvas(sprites: dict[str, Image.Image], content_height: int,
                   margin: int) -> tuple[dict[str, Image.Image], dict[str, dict]]:
    """Scale every pose identically and centre it on one shared baseline."""
    tallest = max(im.height for im in sprites.values())
    scale = content_height / tallest
    scaled = {
        name: im.resize(
            (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
            Image.LANCZOS,
        )
        for name, im in sprites.items()
    }
    cw = max(im.width for im in scaled.values()) + margin * 2
    ch = max(im.height for im in scaled.values()) + margin * 2

    out: dict[str, Image.Image] = {}
    meta: dict[str, dict] = {}
    for name, im in scaled.items():
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        x = (cw - im.width) // 2
        y = ch - margin - im.height  # shared baseline
        canvas.paste(im, (x, y), im)
        out[name] = canvas
        meta[name] = {
            "file": f"{name}.png",
            "canvas": [cw, ch],
            "anchor": [x, y, im.width, im.height],
        }
    return out, meta




# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def _alpha_sprite(sheet: "Image.Image", state: str, profile: dict) -> "Image.Image":
    """Pick the character out of a sheet that already has real transparency.

    Islands below ``min_island_px`` (floating doodles, stray sparkles) are
    dropped, and ``drop_rects`` removes overlay blocks such as the title.
    """
    x0, y0, x1, y1 = profile["boxes"][state]
    crop = sheet.crop((x0, y0, x1, y1)).convert("RGBA")
    w, h = crop.size
    alpha = crop.getchannel("A")
    apx = alpha.load()

    island_drop = [
        (rx0 - x0, ry0 - y0, rx1 - x0, ry1 - y0)
        for rx0, ry0, rx1, ry1 in profile.get("drop_islands_in", [])
    ]
    for rx0, ry0, rx1, ry1 in profile.get("erase_rects", []):
        left, top = max(0, rx0 - x0), max(0, ry0 - y0)
        right, bottom = min(w, rx1 - x0), min(h, ry1 - y0)
        for y in range(top, bottom):
            for x in range(left, right):
                apx[x, y] = 0

    solid = bytearray(w * h)
    for y in range(h):
        base = y * w
        for x in range(w):
            if apx[x, y] > 40:
                solid[base + x] = 1

    minimum = int(profile.get("min_island_px", 400))
    keep = bytearray(w * h)
    seen = bytearray(w * h)
    for sy in range(h):
        for sx in range(w):
            start = sy * w + sx
            if not solid[start] or seen[start]:
                continue
            stack = [(sx, sy)]
            seen[start] = 1
            island = []
            while stack:
                x, y = stack.pop()
                island.append((x, y))
                for dy in (-1, 0, 1):
                    ny = y + dy
                    if not 0 <= ny < h:
                        continue
                    for dx in (-1, 0, 1):
                        nx = x + dx
                        if not 0 <= nx < w:
                            continue
                        j = ny * w + nx
                        if solid[j] and not seen[j]:
                            seen[j] = 1
                            stack.append((nx, ny))
            if len(island) < minimum:
                continue
            ix0 = min(p[0] for p in island)
            ix1 = max(p[0] for p in island)
            iy0 = min(p[1] for p in island)
            iy1 = max(p[1] for p in island)
            if any(rx0 <= ix0 and iy0 >= ry0 and ix1 <= rx1 and iy1 <= ry1
                   for rx0, ry0, rx1, ry1 in island_drop):
                continue  # overlay block such as the sheet title
            for x, y in island:
                keep[y * w + x] = 1

    out = crop.copy()
    opx = out.load()
    for y in range(h):
        base = y * w
        for x in range(w):
            if not keep[base + x]:
                r, g, b, _a = opx[x, y]
                opx[x, y] = (r, g, b, 0)
    bbox = out.getbbox()
    if bbox is None:
        raise SystemExit(f"state {state!r}: no islands large enough in its box")
    return out.crop(bbox)


def _build(sheet_path: str | Path, content_height: int, margin: int):
    path = Path(sheet_path)
    if not path.exists():
        raise FileNotFoundError(f"character sheet not found: {path}")
    profile = profile_for(path)
    if profile.get("mode") == "alpha":
        sheet = Image.open(path).convert("RGBA")
        sprites = {state: _alpha_sprite(sheet, state, profile)
                   for state in profile["boxes"]}
    else:
        sheet = Image.open(path).convert("RGB")
        sprites = {state: Grid(sheet, state, profile).build_sprite()
                   for state in profile["boxes"]}
    return compose_canvas(sprites, content_height, margin)


STATE_BOXES = PROFILES[DEFAULT_PROFILE]["boxes"]


def extract_states(sheet_path: str | Path, content_height: int = 520,
                   margin: int = 6) -> dict[str, Image.Image]:
    """Return {state: RGBA image} for every panel of the sheet.

    All poses end up on one canvas size with a shared baseline, so the caller can
    draw any state into the same rectangle and the character will not move.
    """
    return _build(sheet_path, content_height, margin)[0]


def debug_overlay(sheet_path: str | Path) -> dict[str, Image.Image]:
    """Panel images with removed pixels tinted magenta (development aid)."""
    profile = profile_for(Path(sheet_path))
    if profile.get("mode") == "alpha":
        return {}
    sheet = Image.open(Path(sheet_path)).convert("RGB")
    out: dict[str, Image.Image] = {}
    for state in profile["boxes"]:
        grid = Grid(sheet, state, profile)
        grid.build_sprite()
        panel = grid.img.convert("RGB").copy()
        ppx = panel.load()
        apx = grid.debug_alpha.load()
        bx, by = grid.bbox[0], grid.bbox[1]
        for y in range(grid.debug_alpha.height):
            for x in range(grid.debug_alpha.width):
                if apx[x, y] == 0 and 0 <= bx + x < grid.w and 0 <= by + y < grid.h:
                    r, g, b = ppx[bx + x, by + y]
                    ppx[bx + x, by + y] = (
                        min(255, r // 3 + 170), g // 3, min(255, b // 3 + 90)
                    )
        out[state] = panel
    return out


def write_assets(sheet_path: str | Path, out_dir: str | Path,
                 content_height: int = 520, margin: int = 6,
                 debug: bool = False) -> dict:
    """Slice the sheet into <state>.png files plus manifest.json."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sheet_path = Path(sheet_path)
    canvases, meta = _build(sheet_path, content_height, margin)
    for state, image in canvases.items():
        image.save(out / f"{state}.png")
    manifest = {
        "generated_from": sheet_path.name,
        "mode": "files",
        "states": meta,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if debug:
        for state, panel in debug_overlay(sheet_path).items():
            panel.save(out / f"_debug_{state}.png")
        order = ["listening", "thinking", "thinking_longer",
                 "talking", "proud", "finished"]
        cw, ch = next(iter(canvases.values())).size
        pad = 10
        csheet = Image.new("RGB", (3 * (cw + pad) + pad, 2 * (ch + pad) + pad),
                           (120, 130, 145))
        for i, name in enumerate(order):
            img = canvases[name]
            csheet.paste(img, (pad + (i % 3) * (cw + pad), pad + (i // 3) * (ch + pad)), img)
        csheet.save(out / "_contact_sheet.png")
    return manifest
