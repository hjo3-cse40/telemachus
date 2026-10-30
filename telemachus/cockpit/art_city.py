"""The Golden Hour city: a pixel-art window onto a city by a river, for the Home screen.

The scene is drawn in a pixel buffer and turned into terminal cells with half blocks: each cell is
"▀" with the top pixel as the text color and the bottom pixel as the background (or a space when
both pixels are the same color). A pixel is about square in SF Mono, so a 48 x 16 cell window is
48 x 32 pixels.

The sky follows the clock: day, golden hour, dusk and night are four keyframe palettes, and the
hours between them blend. Every color is an xterm-256 slot (16-255), the same colors theme.py
draws in its 256-color mode, so the result is exact on this Mac's curses.

Pure functions only (no curses): ``render(width, rows, t, hour)`` returns rows of
``(glyph, fg_slot, bg_slot)`` cells; the TUI draws them. ``t`` is animation time in seconds.

The math is a port of the theme board (the same seeded city, the same 32-bit hashes and the same
rounding as JavaScript), so the cockpit shows the scene that was reviewed.
"""
from __future__ import annotations

import math
import time
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

Cell = Tuple[str, int, int]  # (glyph, fg slot, bg slot)

# Sky modes, in the order the s key cycles them. "clock" follows the real time.
SKY_MODES: Tuple[str, ...] = ("clock", "golden", "dusk", "night", "day")
SKY_WORDS: Dict[str, str] = {"clock": "my clock", "golden": "golden hour", "dusk": "dusk", "night": "night", "day": "day"}
SKY_HOURS: Dict[str, float] = {"golden": 17.9, "dusk": 19.15, "night": 22.5, "day": 13.0}
DEFAULT_SKY = "clock"

# ---------------------------------------------------------------------------
# xterm-256 colors
# ---------------------------------------------------------------------------

_CUBE = (0, 95, 135, 175, 215, 255)
_M32 = 0xFFFFFFFF


def slot_rgb(i: int) -> Tuple[int, int, int]:
    if 16 <= i <= 231:
        k = i - 16
        return _CUBE[k // 36], _CUBE[(k // 6) % 6], _CUBE[k % 6]
    v = 8 + 10 * (i - 232)
    return v, v, v


_RGB = {i: slot_rgb(i) for i in range(16, 256)}


@lru_cache(maxsize=8192)
def nearest(r: int, g: int, b: int) -> int:
    """Closest slot in 16-255 with the weighted distance theme.nearest_xterm() uses."""
    best, best_d = 16, float("inf")
    for idx in range(16, 256):
        r2, g2, b2 = _RGB[idx]
        rm = (r + r2) / 2
        d = (2 + rm / 256) * (r - r2) ** 2 + 4 * (g - g2) ** 2 + (2 + (255 - rm) / 256) * (b - b2) ** 2
        if d < best_d:
            best, best_d = idx, d
    return best


def _rnd(x: float) -> int:
    """JavaScript Math.round: halves round up."""
    return math.floor(x + 0.5)


def mix(a: int, b: int, t: float) -> int:
    """Blends two slots in RGB and snaps the result back to a slot."""
    ra, ga, ba = _RGB[a]
    rb, gb, bb = _RGB[b]
    c8 = lambda v: max(0, min(255, _rnd(v)))  # noqa: E731
    return nearest(c8(ra + (rb - ra) * t), c8(ga + (gb - ga) * t), c8(ba + (bb - ba) * t))


# ---------------------------------------------------------------------------
# Sky keyframes (slots) and the clock
# ---------------------------------------------------------------------------

KEYFRAMES: Dict[str, Dict[str, object]] = {
    "day": dict(sky=(67, 68, 74, 110, 117, 153, 189, 195), sun=(231, 230), halo=195, cloud=(254, 231), far=146, mid=103,
                near=60, win=(110, 153), water=(117, 74, 68, 25), glint=231, bird=238, star=None, light=None),
    "golden": dict(sky=(61, 97, 133, 168, 174, 209, 215, 221), sun=(230, 222), halo=216, cloud=(175, 223), far=132, mid=96,
                   near=53, win=(221, 215), water=(174, 132, 96, 53), glint=229, bird=53, star=230, light=215),
    "dusk": dict(sky=(17, 18, 54, 55, 91, 133, 168, 174), sun=(174, 174), halo=174, cloud=(96, 175), far=96, mid=54,
                 near=17, win=(221, 222), water=(133, 54, 18, 17), glint=174, bird=17, star=189, light=221),
    "night": dict(sky=(16, 16, 17, 17, 18, 54, 60, 96), sun=(230, 187), halo=60, cloud=(234, 60), far=236, mid=234,
                  near=16, win=(221, 229), water=(60, 18, 17, 16), glint=221, bird=None, star=255, light=221),
}
LIT = {"day": 0.0, "golden": 0.16, "dusk": 0.42, "night": 0.6}  # share of office windows that are lit
# (hour, keyframe): colors blend between neighbors, then snap to a slot.
STOPS = ((0.0, "night"), (5.4, "night"), (6.4, "golden"), (7.4, "day"), (16.6, "day"), (17.6, "golden"),
         (18.6, "golden"), (19.3, "dusk"), (20.1, "night"), (24.0, "night"))
RED_LIGHT = 196  # the blinking light on the mast
CAR_FRONT, CAR_BACK = 230, 203


def _blend(a: Optional[int], b: Optional[int], t: float) -> Optional[int]:
    if a is None or b is None:
        return a if t < 0.5 else b
    return mix(a, b, t)


@lru_cache(maxsize=2048)
def _palette_cached(minute_key: int) -> Dict[str, object]:
    hour = minute_key / 60.0
    h = hour % 24.0
    i = 0
    while i < len(STOPS) - 2 and h >= STOPS[i + 1][0]:
        i += 1
    (h0, k0), (h1, k1) = STOPS[i], STOPS[i + 1]
    t = 0.0 if h1 == h0 else (h - h0) / (h1 - h0)
    A, B = KEYFRAMES[k0], KEYFRAMES[k1]
    out: Dict[str, object] = {}
    for key, av in A.items():
        bv = B[key]
        if isinstance(av, tuple):
            out[key] = tuple(_blend(x, y, t) for x, y in zip(av, bv))  # type: ignore[arg-type]
        else:
            out[key] = _blend(av, bv, t)  # type: ignore[arg-type]
    out["lit"] = LIT[k0] + (LIT[k1] - LIT[k0]) * t
    if k0 == "night" or k1 == "night":
        out["stars"] = 1.0 if k0 == k1 else (1 - t if k0 == "night" else t)
    else:
        out["stars"] = 0.25 if "dusk" in (k0, k1) else 0.0
    out["key"] = k0 if t < 0.5 else k1
    return out


def palette_at(hour: float) -> Dict[str, object]:
    """Colors for an hour of day (0-24). Cached per minute: the sky changes slowly."""
    return _palette_cached(int(round((hour % 24.0) * 60)) % 1440)


def hour_for(mode: str, now: Optional[float] = None) -> float:
    """Hour of day the sky shows for a mode. "clock" = local time now."""
    if mode in SKY_HOURS:
        return SKY_HOURS[mode]
    lt = time.localtime(time.time() if now is None else now)
    return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0


def next_sky(mode: str) -> str:
    """The sky mode after `mode` in the s-key cycle (unknown modes go back to the start)."""
    if mode not in SKY_MODES:
        return SKY_MODES[0]
    return SKY_MODES[(SKY_MODES.index(mode) + 1) % len(SKY_MODES)]


# ---------------------------------------------------------------------------
# 32-bit helpers (JavaScript semantics, so the seeded city matches the board)
# ---------------------------------------------------------------------------


def _i32(x: int) -> int:
    x &= _M32
    return x - 0x100000000 if x & 0x80000000 else x


def _imul(a: int, b: int) -> int:
    return _i32((a & _M32) * (b & _M32))


def _hash(a: int, b: int, c: int = 0) -> float:
    h = _i32(a * 374761393 + b * 668265263 + c * 2147483647)
    h = _imul(h ^ ((h & _M32) >> 13), 1274126177)
    return ((h ^ ((h & _M32) >> 16)) & _M32) / 4294967296.0


class _Rng:
    """mulberry32, as in the board."""

    def __init__(self, seed: int) -> None:
        self.s = _i32(seed)

    def __call__(self) -> float:
        self.s = _i32(self.s + 0x6D2B79F5)
        s = self.s
        t = _imul(s ^ ((s & _M32) >> 15), 1 | s)
        t = _i32(_i32(t + _imul(t ^ ((t & _M32) >> 7), 61 | t)) ^ t)
        return ((t ^ ((t & _M32) >> 14)) & _M32) / 4294967296.0


# ---------------------------------------------------------------------------
# Pixel buffer
# ---------------------------------------------------------------------------


class _Pix:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.d: List[List[int]] = [[16] * w for _ in range(h)]

    def set(self, x: float, y: float, c: Optional[int]) -> None:
        if c is None:
            return
        xi, yi = math.floor(x), math.floor(y)
        if 0 <= xi < self.w and 0 <= yi < self.h:
            self.d[yi][xi] = c

    def rect(self, x: float, y: float, w: float, h: float, c: Optional[int]) -> None:
        if c is None:
            return
        y0, x0 = max(0, math.floor(y)), max(0, math.floor(x))
        y1, x1 = min(self.h, math.ceil(y + h)), min(self.w, math.ceil(x + w))
        for j in range(y0, y1):
            row = self.d[j]
            for i in range(x0, x1):
                row[i] = c

    def hspan(self, x0: float, x1: float, y: float, c: Optional[int]) -> None:
        if x1 < x0:
            x0, x1 = x1, x0
        for i in range(_rnd(x0), _rnd(x1) + 1):
            self.set(i, y, c)

    def vspan(self, x: float, y0: float, y1: float, c: Optional[int]) -> None:
        if y1 < y0:
            y0, y1 = y1, y0
        for j in range(_rnd(y0), _rnd(y1) + 1):
            self.set(x, j, c)

    def disc(self, cx: float, cy: float, r: float, c: Optional[int]) -> None:
        for y in range(math.floor(cy - r), math.ceil(cy + r) + 1):
            for x in range(math.floor(cx - r), math.ceil(cx + r) + 1):
                dx, dy = x + 0.5 - cx, y + 0.5 - cy
                if dx * dx + dy * dy <= r * r:
                    self.set(x, y, c)

    def cells(self) -> List[List[Cell]]:
        """Half blocks: ▀ = top pixel on bottom pixel; equal pixels = a space on that color."""
        out: List[List[Cell]] = []
        for j in range(self.h // 2):
            top, bot = self.d[2 * j], self.d[2 * j + 1]
            out.append([(" ", t, t) if t == b else ("▀", t, b) for t, b in zip(top, bot)])
        return out


# ---------------------------------------------------------------------------
# The city (seeded, one layout per size)
# ---------------------------------------------------------------------------

_CITY: Dict[Tuple[int, int], Dict[str, object]] = {}


def _city(W: int, H: int) -> Dict[str, object]:
    key = (W, H)
    if key in _CITY:
        return _CITY[key]
    hz = _rnd(H * 0.72)  # the horizon: the water starts here
    u = max(1.0, hz / 23)  # 1 at the cockpit size
    r = _Rng(7 + W * 31 + H)
    far, mid, near, shore = [], [], [], []
    x = -2
    while x < W:
        w = _rnd((2 + r() * 4) * u)
        low = W * 0.5 < x < W * 0.8  # keep the sunset gap clear
        far.append(dict(x=x, w=w, h=_rnd(hz * (0.05 + r() * 0.07 if low else 0.12 + r() * 0.16))))
        x += w
    x = -1
    while x < W:
        w = _rnd((3 + r() * 4) * u)
        f = x / W
        hf = 0.03 + r() * 0.04 if f > 0.74 else (0.08 + r() * 0.1 if f > 0.5 else 0.22 + r() * 0.3)
        mid.append(dict(x=x, w=w, h=_rnd(hz * hf), step=r() < 0.4, win=r() < 0.5))
        x += w + (_rnd(u) if r() < 0.3 else 0)

    def add(fx: float, w: float, hf: float, kind: str) -> None:
        near.append(dict(x=_rnd(W * fx), w=max(2, _rnd(w * u * (0.85 if u > 1.5 else 1))), h=_rnd(hz * hf), kind=kind,
                         pat=math.floor(r() * 3)))

    add(0.015, 4, 0.3, "tank")
    add(0.085, 3, 0.5, "box")
    add(0.135, 4, 0.82, "spire")
    add(0.215, 3, 0.56, "box")
    add(0.285, 5, 0.64, "cake")
    add(0.37, 2, 0.42, "box")
    add(0.415, 4, 0.72, "antenna")
    add(0.485, 3, 0.38, "box")
    x = 0
    while x < W * 0.74:
        w = _rnd((2 + r() * 4) * u)
        shore.append(dict(x=x, w=w, h=max(1, _rnd(hz * (0.05 + r() * 0.07)))))
        x += w
    bridge = dict(tower=_rnd(W * 0.84), anchor=_rnd(W * 0.735), deck=hz - max(2, _rnd(1.5 * u)), top=_rnd(hz * 0.38),
                  tw=max(1, _rnd(u)))
    stars = [dict(x=math.floor(r() * W), y=math.floor(r() * hz * 0.55), k=r()) for _ in range(_rnd(W * H * 0.012))]
    city = dict(W=W, H=H, hz=hz, u=u, far=far, mid=mid, near=near, shore=shore, bridge=bridge, stars=stars)
    _CITY[key] = city
    return city


# ---------------------------------------------------------------------------
# The scene
# ---------------------------------------------------------------------------


def scene(pix: _Pix, t: float, hour: float) -> Dict[str, object]:
    """Draws the city into `pix` at animation time t for an hour of day. Returns the palette."""
    W, H = pix.w, pix.h
    cty = _city(W, H)
    hz, u = cty["hz"], cty["u"]  # type: ignore[assignment]
    P = palette_at(hour)
    sky: Sequence[int] = P["sky"]  # type: ignore[assignment]
    h = hour % 24.0
    n_bands = len(sky)
    band_h = hz / n_bands

    # sky: 8 bands; at each edge a sparse ordered dither (1 pixel in 4) on both sides blends them
    for y in range(hz):
        b = min(n_bands - 1, math.floor(y / band_h))
        last_row = b < n_bands - 1 and y == math.floor((b + 1) * band_h) - 1
        first_row = b > 0 and y == math.floor(b * band_h)
        row = pix.d[y]
        base, nxt, prv = sky[b], sky[min(n_bands - 1, b + 1)], sky[max(0, b - 1)]
        odd = 2 * (y & 1)
        for x in range(W):
            c = base
            if last_row and (x + odd) % 4 == 0:
                c = nxt
            elif first_row and (x + odd) % 4 == 2:
                c = prv
            row[x] = c

    def sky_at(y: float) -> int:
        return sky[max(0, min(n_bands - 1, math.floor(y / band_h)))]

    # stars, and the evening star at golden hour
    star = P["star"]
    if star is not None:
        if P["stars"] > 0:  # type: ignore[operator]
            for s in cty["stars"]:  # type: ignore[union-attr]
                if s["k"] > P["stars"]:
                    continue
                if math.floor(t * 1.5 + s["k"] * 50) % 9 == 0:
                    continue  # twinkle
                pix.set(s["x"], s["y"], star if s["k"] < 0.15 else mix(star, sky_at(s["y"]), 0.45))  # type: ignore[arg-type]
        if P["key"] == "golden":
            pix.set(_rnd(W * 0.2), _rnd(hz * 0.12), star)  # type: ignore[arg-type]

    # sun (06:00-19:00) or moon
    sun_c: Tuple[int, int] = P["sun"]  # type: ignore[assignment]
    alt = math.sin(math.pi * (h - 6.5) / 12)
    if 6 < h < 19:
        rr = max(3.0, 3.4 * u)
        sx = W * (0.1 + 0.56 * (h - 6.5) / 12)
        sy = hz - 3 * u - rr * 0.4 - alt * hz * 0.92
        halo = P["halo"]
        for y in range(math.floor(sy - rr - 2), math.floor(sy + rr + 2) + 1):
            for x in range(math.floor(sx - rr - 2), math.floor(sx + rr + 2) + 1):
                d = math.hypot(x + 0.5 - sx, y + 0.5 - sy)
                if rr < d <= rr + 1.6 and (x + y) % 2 == 0 and y < hz:
                    pix.set(x, y, halo)  # type: ignore[arg-type]
        pix.disc(sx, sy, rr, sun_c[1])
        pix.disc(sx, sy, rr * 0.72, sun_c[0])
        sun = dict(x=sx, r=rr, up=True)
    else:
        sun = dict(x=W * 0.24, r=2.5 * u, up=False)
        if h >= 19.6 or h < 5.6:
            mx, my, mr = W * 0.24, hz * 0.22, max(2.0, 2.4 * u)
            pix.disc(mx, my, mr, sun_c[0])
            pix.disc(mx + mr * 0.55, my - mr * 0.25, mr * 0.9, sky_at(my))

    # clouds: long thin banks, stepped ends, lit from below, drifting right
    cloud: Tuple[int, int] = P["cloud"]  # type: ignore[assignment]
    row_h = max(1, _rnd(u * 0.7))
    shape = ((0.35, 0.7), (0.12, 0.92), (0.0, 1.0), (0.2, 0.78))
    for i, (fx, fy, length) in enumerate(((0.06, 0.26, 18), (0.5, 0.17, 26), (0.8, 0.36, 13))):
        L = _rnd(length * u)
        y0 = _rnd(hz * fy)
        x0 = _rnd(((fx * W + t * (0.3 + i * 0.1) * u) % (W + L * 2)) - L)
        for j, (a, b) in enumerate(shape):
            for dy in range(row_h):
                lit = j >= len(shape) - 2 and (j == len(shape) - 1 or dy == row_h - 1)
                pix.hspan(x0 + a * L, x0 + b * L, y0 + j * row_h + dy, cloud[1] if lit else cloud[0])

    # far and mid skyline
    far_c, mid_c, near_c = P["far"], P["mid"], P["near"]
    win: Tuple[int, int] = P["win"]  # type: ignore[assignment]
    lit_share: float = P["lit"]  # type: ignore[assignment]
    for b in cty["far"]:  # type: ignore[union-attr]
        pix.rect(b["x"], hz - b["h"], b["w"], b["h"], far_c)  # type: ignore[arg-type]
    for b in cty["mid"]:  # type: ignore[union-attr]
        pix.rect(b["x"], hz - b["h"], b["w"], b["h"], mid_c)  # type: ignore[arg-type]
        if b["step"] and b["w"] > 3:
            step = max(1, _rnd(u))
            pix.rect(b["x"] + 1, hz - b["h"] - step, b["w"] - 2, step, mid_c)  # type: ignore[arg-type]
        if b["win"] and lit_share > 0.1 and b["h"] > 4:
            for wy in range(hz - b["h"] + 2, hz - 2, 3):
                for wx in range(b["x"] + 1, b["x"] + b["w"] - 1, 2):
                    if _hash(wx, wy, 7) < lit_share * 0.55:
                        pix.set(wx, wy, win[1])

    # near skyline: landmarks, setbacks, windows
    for b in cty["near"]:  # type: ignore[union-attr]
        top = hz - b["h"]
        s = max(1, _rnd(u * 0.6)) if b["h"] > hz * 0.45 and b["w"] >= 5 else 0
        cut = top + _rnd(b["h"] * 0.4)
        ux, uw = b["x"] + s, b["w"] - 2 * s
        y_start = cut if s else top
        pix.rect(b["x"], y_start, b["w"], hz - y_start, near_c)  # type: ignore[arg-type]
        if s:
            pix.rect(ux, top, uw, cut - top, near_c)  # type: ignore[arg-type]
        if b["kind"] in ("spire", "cake"):
            s1 = max(1, _rnd(1.6 * u))
            pix.rect(ux + 1, top - s1, uw - 2, s1, near_c)  # type: ignore[arg-type]
            pix.rect(ux + 2, top - 2 * s1, max(1, uw - 4), s1, near_c)  # type: ignore[arg-type]
            if b["kind"] == "spire":
                pix.vspan(ux + uw // 2, top - 2 * s1 - _rnd(4 * u), top - 2 * s1, near_c)  # type: ignore[arg-type]
        if b["kind"] == "antenna":
            ah, ax = _rnd(5 * u), ux + uw // 2
            pix.vspan(ax, top - ah, top, near_c)  # type: ignore[arg-type]
            if P["light"] is not None and math.floor(t) % 2 == 0:
                pix.set(ax, top - ah, RED_LIGHT)
        if b["kind"] == "tank":
            tw = max(3, _rnd(3 * u))
            tx, ty = b["x"] + 1, top - _rnd(2 * u) - tw
            pix.rect(tx, ty, tw, tw, near_c)  # type: ignore[arg-type]
            pix.hspan(tx + 1, tx + tw - 2, ty - 1, near_c)  # type: ignore[arg-type]
            pix.vspan(tx, ty + tw, top, near_c)  # type: ignore[arg-type]
            pix.vspan(tx + tw - 1, ty + tw, top, near_c)  # type: ignore[arg-type]
        # windows: a grid, lit floors or lit columns (pattern per tower); a few switch slowly
        step_x = 1 if b["pat"] == 2 and u > 1.5 else 2
        step_y = 3 if b["pat"] == 1 and u > 1.5 else 2
        for wy in range(top + 2, hz - 1, step_y):
            upper = s and wy < cut
            x_lo = (ux if upper else b["x"]) + 1
            x_hi = (ux + uw if upper else b["x"] + b["w"]) - 1
            for wx in range(x_lo, x_hi, step_x):
                if b["pat"] == 2:
                    k = _hash(b["x"], wy, W)
                elif b["pat"] == 1:
                    k = _hash(wx, b["x"], W) * 0.7 + _hash(wx, wy, W) * 0.3
                else:
                    k = _hash(wx, wy, W)
                flip = _hash(wx, wy, 99) < 0.05 and math.floor(t / 6 + k * 12) % 2 == 0
                if k < lit_share or flip:
                    pix.set(wx, wy, win[1] if _hash(wx, wy, 3) < 0.4 else win[0])

    # the waterfront strip in front of the towers
    for s in cty["shore"]:  # type: ignore[union-attr]
        pix.rect(s["x"], hz - s["h"], s["w"], s["h"], near_c)  # type: ignore[arg-type]
    if lit_share > 0.1:
        for s in cty["shore"]:  # type: ignore[union-attr]
            if s["h"] > 1 and _hash(s["x"], 11) < lit_share:
                pix.set(s["x"] + 1, hz - s["h"] + 1, win[0])

    # the suspension bridge: tower, cables, hangers, deck, deck lights, cars
    br = cty["bridge"]
    for i in range(br["tw"]):  # type: ignore[index]
        pix.vspan(br["tower"] + i, br["top"], hz, near_c)  # type: ignore[index, arg-type]
    pix.hspan(br["anchor"], W, br["deck"], near_c)  # type: ignore[index, arg-type]

    def cable(x: float) -> float:
        if x < br["tower"]:  # type: ignore[index]
            f = (x - br["anchor"]) / (br["tower"] - br["anchor"])  # type: ignore[index]
            return br["deck"] - (br["deck"] - br["top"]) * f * f  # type: ignore[index]
        span = (W - br["tower"]) * 1.6  # type: ignore[index]
        f = (x - br["tower"]) / span  # type: ignore[index]
        return br["top"] + (br["deck"] - 1 - br["top"]) * math.sin(math.pi * min(1.0, f))  # type: ignore[index]

    hanger = max(2, _rnd(2 * u))
    for x in range(br["anchor"], W):  # type: ignore[index]
        y = _rnd(cable(x))
        pix.set(x, y, near_c)  # type: ignore[arg-type]
        if x % hanger == 0:
            pix.vspan(x, y, br["deck"], near_c)  # type: ignore[index, arg-type]
    light = P["light"]
    if light is not None:
        for x in range(br["anchor"] + 1, W, max(3, _rnd(3 * u))):  # type: ignore[index]
            pix.set(x, br["deck"] - 1, light)  # type: ignore[index, arg-type]
        for k in range(3):
            span = W - br["anchor"] + 8  # type: ignore[index]
            p = (t * (2.5 + k) * u + k * 17) % span
            right = k % 2 == 0
            cx = br["anchor"] + p - 4 if right else W - p + 4  # type: ignore[index]
            if br["anchor"] <= cx < W:  # type: ignore[index]
                pix.set(_rnd(cx), br["deck"], CAR_FRONT if right else CAR_BACK)  # type: ignore[index]

    # water: bands, slow ripples, the sun's (or moon's) path, window light streaks
    water: Tuple[int, ...] = P["water"]  # type: ignore[assignment]
    glint = P["glint"]
    depth = H - hz
    for y in range(hz, H):
        d = (y - hz) / depth
        band = water[min(len(water) - 1, math.floor(d * len(water)))]
        pix.d[y] = [band] * W
    for y in range(hz + 1, H, 2):
        d = (y - hz) / depth
        band = min(len(water) - 1, math.floor(d * len(water)))
        hi = water[band - 1] if band > 0 else glint
        off = math.floor(t * (0.6 + d)) + y * 7
        row = pix.d[y]
        for x in range(W):
            if _hash(math.floor((x + off) / 3), y, 5) < 0.08:
                row[x] = hi  # type: ignore[assignment]
    for y in range(hz, H):
        d, j = (y - hz) / depth, y - hz
        if j % 2 == 1 and d > 0.25:
            continue  # gaps between streaks further out
        wob = math.sin(t * 2.2 + j * 1.7) * 0.5 + math.sin(t * 1.3 + j * 0.9) * 0.5
        half = max(0.5, sun["r"] * (1.05 if sun["up"] else 0.55) * (1 - d * 0.6) * (0.75 + 0.25 * wob))
        shift = _rnd(math.sin(t * 0.9 + j) * min(1.5, 0.4 * u) * d)
        pix.hspan(sun["x"] - half + shift, sun["x"] + half + shift, y,
                  sun_c[0] if j < 2 or (j % 3 == 0 and d < 0.5) else glint)  # type: ignore[arg-type]
    if lit_share > 0.3:
        for b in cty["near"]:  # type: ignore[union-attr]
            if _hash(b["x"], 5) > lit_share:
                continue
            x = b["x"] + b["w"] // 2
            for y in range(hz + 1, H - 1, 2):
                if _hash(x, y, math.floor(t * 2)) > 0.45:
                    pix.set(x, y, win[0])

    # birds: a small flock crossing now and then
    bird = P["bird"]
    if bird is not None:
        p = t % 46
        if p < 30:
            for i in range(3):
                bx = _rnd((p / 30) * (W + 12)) - 6 - i * 4
                by = _rnd(hz * 0.42 + i * 1.6 * u - math.sin(p + i) * 0.6)
                if math.floor(t * 3 + i) % 2 == 0:
                    pix.set(bx, by, bird)  # type: ignore[arg-type]
                    pix.set(bx + 1, by + 1, bird)  # type: ignore[arg-type]
                    pix.set(bx + 2, by, bird)  # type: ignore[arg-type]
                else:
                    for dx in range(3):
                        pix.set(bx + dx, by + 1, bird)  # type: ignore[arg-type]
    return P


def render(width: int, rows: int, t: float, hour: float) -> List[List[Cell]]:
    """The scene as `rows` rows of `width` cells (2 pixels per cell vertically)."""
    if width <= 0 or rows <= 0:
        return []
    pix = _Pix(width, rows * 2)
    scene(pix, t, hour)
    return pix.cells()
