"""X11 input + clipboard + screenshot helpers (xdotool / xclip / ImageMagick).

Everything goes through the virtual X display started by runtime.py.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

DISPLAY = os.environ.get("DISPLAY", ":99")


def _env():
    e = dict(os.environ)
    e["DISPLAY"] = DISPLAY
    return e


def run(cmd: list, timeout: float = 10, check: bool = False, input_bytes: Optional[bytes] = None) -> str:
    p = subprocess.run(cmd, env=_env(), capture_output=True, timeout=timeout, input=input_bytes)
    if check and p.returncode != 0:
        raise RuntimeError(f"{shlex.join(cmd)} failed: {p.stderr.decode(errors='replace')[:400]}")
    return p.stdout.decode(errors="replace")


def key(*combos: str, delay_ms: int = 30):
    """xdotool key names, e.g. key('ctrl+v'), key('Return'), key('Escape')."""
    run(["xdotool", "key", "--delay", str(delay_ms), *combos])


def move(x: int, y: int):
    run(["xdotool", "mousemove", str(int(x)), str(int(y))])


def click(x: int, y: int, button: int = 1, repeat: int = 1):
    run(["xdotool", "mousemove", str(int(x)), str(int(y)), "sleep", "0.05",
         "click", "--repeat", str(repeat), "--delay", "80", str(button)])


def double_click(x: int, y: int):
    click(x, y, 1, 2)


def right_click(x: int, y: int):
    click(x, y, 3, 1)


def scroll(x: int, y: int, clicks: int):
    """Positive = scroll up (older history), negative = down."""
    btn = "4" if clicks > 0 else "5"
    run(["xdotool", "mousemove", str(int(x)), str(int(y)), "click", "--repeat", str(abs(clicks)),
         "--delay", "40", btn])


def type_ascii(s: str):
    run(["xdotool", "type", "--delay", "20", "--", s])


_clip_proc: Optional[subprocess.Popen] = None


def _clip(data: bytes, target: str):
    """Own the CLIPBOARD selection with the given data/target (xclip stays alive to serve it)."""
    global _clip_proc
    if _clip_proc and _clip_proc.poll() is None:
        try:
            _clip_proc.kill()
        except Exception:
            pass
    p = subprocess.Popen(["xclip", "-selection", "clipboard", "-t", target, "-i", "-loops", "0"],
                         env=_env(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    p.stdin.write(data)
    p.stdin.close()
    _clip_proc = p
    time.sleep(0.15)


def set_clipboard_text(s: str):
    _clip(s.encode("utf-8"), "UTF8_STRING")


def set_clipboard_image(path: str):
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp",
            "webp": "image/webp"}.get(ext, "image/png")
    _clip(p.read_bytes(), mime)


def set_clipboard_files(paths: list):
    uris = "\r\n".join("file://" + quote(str(Path(x).resolve())) for x in paths) + "\r\n"
    _clip(uris.encode(), "text/uri-list")


def get_clipboard_text() -> str:
    return run(["xclip", "-selection", "clipboard", "-o"], timeout=3)


def screenshot(path: Optional[str] = None, crop: Optional[tuple] = None, scale: Optional[float] = None) -> bytes:
    """PNG bytes of the root window (optionally cropped to (x, y, w, h))."""
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cmd = ["import", "-silent", "-window", "root"]
        if crop:
            x, y, w, h = crop
            cmd += ["-crop", f"{int(w)}x{int(h)}+{int(x)}+{int(y)}", "+repage"]
        if scale and scale != 1:
            cmd += ["-resize", f"{int(scale * 100)}%"]
        cmd.append(tmp)
        run(cmd, timeout=20, check=True)
        data = Path(tmp).read_bytes()
        if path:
            Path(path).write_bytes(data)
        return data
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def active_window() -> str:
    return run(["xdotool", "getactivewindow"], timeout=3).strip()


def search_windows(name: str) -> list:
    out = run(["xdotool", "search", "--name", name], timeout=5)
    return [w for w in out.split() if w.strip()]


def activate_window(wid: str):
    run(["xdotool", "windowactivate", "--sync", wid], timeout=5)


def window_geometry(wid: str) -> dict:
    out = run(["xdotool", "getwindowgeometry", "--shell", wid], timeout=3)
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


def move_resize(wid: str, x: int, y: int, w: int, h: int):
    run(["xdotool", "windowmove", wid, str(x), str(y)], timeout=3)
    run(["xdotool", "windowsize", wid, str(w), str(h)], timeout=3)
