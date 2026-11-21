"""Tiny pure-Python image helpers over a raw RGB screenshot (no PIL needed)."""
from __future__ import annotations

import os
import subprocess


class Shot:
    """An RGB screenshot of the root window, addressable by screen pixel."""

    def __init__(self, display: str | None = None):
        env = dict(os.environ)
        if display:
            env["DISPLAY"] = display
        raw = subprocess.run(["import", "-silent", "-window", "root", "-depth", "8", "ppm:-"],
                             env=env, capture_output=True, timeout=20).stdout
        # parse binary PPM (P6)
        parts = []
        idx = 0
        while len(parts) < 4:
            while raw[idx:idx + 1].isspace():
                idx += 1
            if raw[idx:idx + 1] == b"#":
                while raw[idx:idx + 1] not in (b"\n", b""):
                    idx += 1
                continue
            start = idx
            while not raw[idx:idx + 1].isspace():
                idx += 1
            parts.append(raw[start:idx])
        idx += 1
        assert parts[0] == b"P6", parts[0]
        self.w, self.h = int(parts[1]), int(parts[2])
        self.data = raw[idx:]

    def px(self, x: int, y: int) -> tuple:
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return (0, 0, 0)
        i = (y * self.w + x) * 3
        d = self.data
        return d[i], d[i + 1], d[i + 2]

    def patch(self, x: int, y: int, w: int, h: int, step: int = 2) -> list:
        return [self.px(xx, yy) for yy in range(y, y + h, step) for xx in range(x, x + w, step)]

    def busy(self, x: int, y: int, w: int, h: int, bg: tuple | None = None, tol: int = 18) -> float:
        """Fraction of pixels in the box that differ from the background colour."""
        pts = self.patch(x, y, w, h)
        if not pts:
            return 0.0
        if bg is None:
            bg = self.px(x - 6, y + h // 2)
        diff = sum(1 for p in pts if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > tol)
        return diff / len(pts)

    def ahash(self, x: int, y: int, w: int, h: int, n: int = 8) -> int:
        """Average-hash of a region (used to recognise avatars)."""
        vals = []
        for j in range(n):
            for i in range(n):
                xx = x + int((i + 0.5) * w / n)
                yy = y + int((j + 0.5) * h / n)
                r, g, b = self.px(xx, yy)
                vals.append(r * 299 + g * 587 + b * 114)
        avg = sum(vals) / len(vals)
        bits = 0
        for v in vals:
            bits = (bits << 1) | (1 if v > avg else 0)
        return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")
