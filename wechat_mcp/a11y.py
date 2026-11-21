"""Thin helpers over AT-SPI2 (pyatspi) for reading and poking the WeChat Qt UI.

All functions here are synchronous and must be called from the single UI worker
thread (see ui.py) because pyatspi/D-Bus objects are not thread-safe.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import pyatspi

Node = "pyatspi.Accessible"


def desktop():
    return pyatspi.Registry.getDesktop(0)


def wechat_app():
    """Return the accessible application object of WeChat, or None."""
    d = desktop()
    for i in range(d.childCount):
        try:
            a = d.getChildAtIndex(i)
            if a is not None and a.name in ("wechat", "weixin", "WeChat", "微信"):
                return a
        except Exception:
            continue
    return None


def frames(app=None) -> list:
    app = app or wechat_app()
    if app is None:
        return []
    out = []
    for i in range(app.childCount):
        try:
            c = app.getChildAtIndex(i)
            if c is not None:
                out.append(c)
        except Exception:
            pass
    return out


def role(n) -> str:
    try:
        return n.getRoleName()
    except Exception:
        return "?"


def name(n) -> str:
    try:
        return n.name or ""
    except Exception:
        return ""


def children(n) -> list:
    out = []
    try:
        cnt = n.childCount
    except Exception:
        return out
    for i in range(min(cnt, 5000)):
        try:
            c = n.getChildAtIndex(i)
            if c is not None:
                out.append(c)
        except Exception:
            pass
    return out


CONTAINER_ROLES = ("list", "table", "tree", "tree table")


def walk(root, max_depth: int = 40, prune_hidden: bool = False, no_descend: tuple = ()) -> Iterator[tuple]:
    """Depth-first (node, depth) generator.

    prune_hidden: do not descend into subtrees that are not showing (hidden tabs/pages).
    no_descend: roles whose children are skipped (e.g. long lists) — the node itself is yielded.
    """
    stack = [(root, 0)]
    while stack:
        n, d = stack.pop()
        yield n, d
        if d >= max_depth:
            continue
        if prune_hidden and d >= 2 and not is_showing(n):
            continue
        if no_descend and d > 0 and role(n) in no_descend:
            continue
        kids = children(n)
        for c in reversed(kids):
            stack.append((c, d + 1))


def find_all(root, role_name: Optional[str] = None, name_eq: Optional[str] = None,
             name_in: Optional[tuple] = None, pred: Optional[Callable] = None,
             max_depth: int = 40, limit: int = 10000, prune_hidden: bool = True,
             into_lists: Optional[bool] = None) -> list:
    """Find nodes. By default hidden subtrees are skipped, and list contents are only searched
    when looking for list items (into_lists overrides)."""
    if into_lists is None:
        into_lists = role_name in (None, "list item", "table cell", "tree item")
    out = []
    for n, _ in walk(root, max_depth, prune_hidden=prune_hidden,
                     no_descend=() if into_lists else CONTAINER_ROLES):
        if role_name is not None and role(n) != role_name:
            continue
        nm = None
        if name_eq is not None:
            nm = name(n)
            if nm != name_eq:
                continue
        if name_in is not None:
            nm = nm if nm is not None else name(n)
            if nm not in name_in:
                continue
        if pred is not None and not pred(n):
            continue
        out.append(n)
        if len(out) >= limit:
            break
    return out


def find_one(root, **kw):
    r = find_all(root, limit=1, **kw)
    return r[0] if r else None


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def visible(self) -> bool:
        return self.w > 0 and self.h > 0


def box(n) -> Box:
    try:
        e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        return Box(e.x, e.y, e.width, e.height)
    except Exception:
        return Box(0, 0, 0, 0)


def states(n) -> set:
    try:
        ss = n.getState()
        return {pyatspi.stateToString(s) for s in ss.getStates()}
    except Exception:
        return set()


def is_showing(n) -> bool:
    st = states(n)
    return "showing" in st and "visible" in st


def text(n) -> str:
    """Best-effort textual content: Text interface, then name, then description."""
    try:
        t = n.queryText()
        s = t.getText(0, -1)
        if s:
            return s
    except Exception:
        pass
    nm = name(n)
    if nm:
        return nm
    try:
        return n.description or ""
    except Exception:
        return ""


def all_text(n, max_depth: int = 8, sep: str = " | ") -> str:
    parts = []
    seen = set()
    for c, _ in walk(n, max_depth):
        s = text(c).strip()
        if s and s not in seen:
            seen.add(s)
            parts.append(s)
    return sep.join(parts)


def actions(n) -> list:
    try:
        a = n.queryAction()
        return [a.getName(i) for i in range(a.nActions)]
    except Exception:
        return []


def do_action(n, preferred=("Press", "press", "click", "Click", "activate", "SetFocus")) -> bool:
    try:
        a = n.queryAction()
        names = [a.getName(i) for i in range(a.nActions)]
        for p in preferred:
            if p in names:
                return bool(a.doAction(names.index(p)))
        if a.nActions:
            return bool(a.doAction(0))
    except Exception:
        pass
    return False


def set_text(n, value: str) -> bool:
    try:
        et = n.queryEditableText()
        et.setTextContents(value)
        return True
    except Exception:
        return False


def grab_focus(n) -> bool:
    try:
        return bool(n.queryComponent().grabFocus())
    except Exception:
        return False


def selection_select(list_node, index: int) -> bool:
    try:
        return bool(list_node.querySelection().selectChild(index))
    except Exception:
        return False


def dump(root, max_depth: int = 30, with_box: bool = True, only_showing: bool = False,
         max_nodes: int = 4000) -> str:
    lines = []
    for n, d in walk(root, max_depth):
        if only_showing and d > 0 and not is_showing(n):
            continue
        r = role(n)
        nm = name(n)
        t = ""
        try:
            tt = n.queryText().getText(0, 200)
            if tt and tt != nm:
                t = f" text={tt!r}"
        except Exception:
            pass
        b = ""
        if with_box:
            bx = box(n)
            b = f" @{bx.x},{bx.y} {bx.w}x{bx.h}"
        lines.append("  " * d + f"[{r}] {nm!r}{t}{b}")
        if len(lines) >= max_nodes:
            lines.append("... truncated")
            break
    return "\n".join(lines)


def wait_for(fn: Callable, timeout: float = 5.0, interval: float = 0.15):
    """Poll fn() until it returns a truthy value or timeout; return the value or None."""
    end = time.time() + timeout
    while True:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        if time.time() >= end:
            return None
        time.sleep(interval)
