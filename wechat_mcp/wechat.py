"""High-level WeChat operations implemented on top of the official Linux client's UI.

Everything here runs on the single UI thread (see ui.py). Coordinates: AT-SPI
reports logical (unscaled) coordinates; the X screen is scaled by `self.R`.
Popup windows report coordinates relative to their own window, so they are
mapped through the X window geometry (see `_origin`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

from . import a11y, xinput
from .pixels import Shot, hamming

log = logging.getLogger("wechat_mcp.wechat")

HOME = Path(os.environ.get("WECHAT_MCP_HOME", "/data"))
OUTBOX = HOME / "outbox"
STATE_FILE = HOME / "state.json"

TIME_RE = re.compile(
    r"^((昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\s*)?"
    r"((\d{4}年)?\d{1,2}月\d{1,2}日\s*)?((上午|下午|凌晨|中午|晚上)\s*)?\d{1,2}:\d{2}$|"
    r"^(Yesterday|Today|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+\d{1,2}:\d{2}$|"
    r"^\d{1,2}/\d{1,2}(/\d{2,4})?\s+\d{1,2}:\d{2}$")
GROUP_TITLE_RE = re.compile(r"^(.*)\((\d+)\)$")


class WeChatError(RuntimeError):
    pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(d: dict):
    try:
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    except Exception:
        pass


class WeChat:
    def __init__(self):
        self.R = 1.0
        self.avatar_cache: dict[str, list] = {}   # chat -> [(hash, name)]
        self.last_prompt_at = 0.0
        self.prompt_count = 0
        self.last_login_state: dict = {}
        self.events: list = []                    # recent auto-actions (login watcher etc.)
        self.state = _load_state()

    # ================================================================ basics
    def app(self):
        a = a11y.wechat_app()
        if a is None:
            raise WeChatError("WeChat client is not running (it is being restarted, try again in ~20s)")
        return a

    def main_frame(self):
        mf = getattr(self, "_mf", None)
        if mf is not None:
            try:
                if a11y.box(mf).w > 0 and a11y.find_one(mf, role_name="tool bar", name_in=("导航", "Navigation"), max_depth=6):
                    return mf
            except Exception:
                pass
        self._mf = self._find_main_frame()
        return self._mf

    def _find_main_frame(self):
        for f in a11y.frames(self.app()):
            if a11y.find_one(f, role_name="tool bar", name_in=("导航", "Navigation"), max_depth=8):
                return f
        return None

    def is_logged_in(self) -> bool:
        try:
            return self.main_frame() is not None
        except WeChatError:
            return False

    def require_login(self):
        if not self.is_logged_in():
            st = self.login_state()
            raise WeChatError(f"WeChat is not logged in (state: {st.get('state')}: {st.get('message')}). "
                              "Call wechat_login to get the QR code / login page.")
        self.fix_window()

    def fix_window(self):
        """Keep the main window at 0,0 filling the screen; recompute the scale ratio."""
        f = self.main_frame()
        if f is None:
            return
        try:
            wins = xinput.search_windows("^微信$") or xinput.search_windows("^WeChat$")
            if wins:
                wid = wins[0]
                g = xinput.window_geometry(wid)
                sw, sh = self.screen_size()
                if int(g.get("X", 0)) != 0 or int(g.get("Y", 0)) != 0 or int(g.get("WIDTH", 0)) < sw - 4:
                    xinput.move_resize(wid, 0, 0, sw, sh)
                    time.sleep(0.6)
                    g = xinput.window_geometry(wid)
                b = a11y.box(f)
                if b.w > 0:
                    self.R = int(g["WIDTH"]) / b.w
        except Exception as e:  # pragma: no cover
            log.debug("fix_window: %s", e)

    def screen_size(self) -> tuple:
        out = xinput.run(["xdpyinfo"], timeout=5)
        m = re.search(r"dimensions:\s+(\d+)x(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (1440, 900)

    def shown(self, role: str, name: Optional[str] = None, root=None, pred=None) -> list:
        root = root or self.app()
        return [n for n in a11y.find_all(root, role_name=role, name_eq=name, pred=pred)
                if a11y.is_showing(n) and a11y.box(n).w > 0]

    def shown_one(self, role: str, name: Optional[str] = None, root=None, pred=None):
        r = self.shown(role, name, root, pred)
        return r[0] if r else None

    # ----------------------------------------------------------- coordinates
    def _frame_of(self, node):
        n = node
        for _ in range(60):
            try:
                p = n.parent
            except Exception:
                return None
            if p is None:
                return None
            if a11y.role(p) == "application":
                return n
            n = p
        return None

    def _origin(self, frame) -> tuple:
        """Screen origin of a frame. Popups report (0,0)-relative boxes; find their X window."""
        fb = a11y.box(frame)
        mf = self.main_frame()
        if fb.x != 0 or fb.y != 0 or (mf is not None and a11y.box(mf).w == fb.w and a11y.box(mf).h == fb.h):
            return (0, 0, False)
        want_w, want_h = round(fb.w * self.R), round(fb.h * self.R)
        out = xinput.run(["xdotool", "search", "--onlyvisible", "--name", ""], timeout=5)
        best = None
        for wid in out.split():
            g = xinput.window_geometry(wid)
            try:
                w, h = int(g["WIDTH"]), int(g["HEIGHT"])
            except Exception:
                continue
            if abs(w - want_w) <= 4 and abs(h - want_h) <= 4 and (int(g["X"]) or int(g["Y"])):
                best = (int(g["X"]), int(g["Y"]))
        if best:
            return (best[0], best[1], True)
        return (0, 0, False)

    def screen_xy(self, node, fx: float = 0.5, fy: float = 0.5) -> tuple:
        b = a11y.box(node)
        fr = self._frame_of(node)
        ox, oy, rel = self._origin(fr) if fr is not None else (0, 0, False)
        lx = b.x + b.w * fx
        ly = b.y + b.h * fy
        if rel:
            fb = a11y.box(fr)
            return int(ox + (lx - fb.x) * self.R), int(oy + (ly - fb.y) * self.R)
        return int(lx * self.R), int(ly * self.R)

    def activate_frame(self, fr):
        """Raise the X window of a frame (dialogs can end up behind the main window)."""
        try:
            nm = a11y.name(fr)
            mf = self.main_frame()
            if mf is not None and fr == mf:
                wins = xinput.search_windows("^微信$") or xinput.search_windows("^WeChat$")
            elif nm:
                wins = xinput.search_windows("^" + re.escape(nm) + "$")
            else:
                return
            if wins and xinput.active_window() != wins[-1]:
                xinput.run(["xdotool", "windowactivate", wins[-1]], timeout=3)
                time.sleep(0.25)
        except Exception:
            pass

    def click(self, node, button: int = 1, fx: float = 0.5, fy: float = 0.5, double: bool = False):
        fr = self._frame_of(node)
        if fr is not None:
            self.activate_frame(fr)
        x, y = self.screen_xy(node, fx, fy)
        if double:
            xinput.double_click(x, y)
        else:
            xinput.click(x, y, button)

    def popups(self) -> list:
        """Frames other than the main window (menus, dialogs, profile cards)."""
        mf = self.main_frame()
        out = []
        for f in a11y.frames(self.app()):
            if mf is not None and f == mf:
                continue
            if a11y.box(f).w <= 0:
                continue
            nm = a11y.name(f)
            if nm and not xinput.search_windows("^" + re.escape(nm) + "$"):
                continue   # stale accessible frame of a closed window
            out.append(f)
        return out

    def close_popups(self, rounds: int = 2):
        for _ in range(rounds):
            ps = self.popups()
            if not ps:
                break
            self.activate_frame(ps[-1]) if a11y.name(ps[-1]) else None
            xinput.key("Escape")
            time.sleep(0.35)
        mf = self.main_frame()
        if mf is not None:
            self.activate_frame(mf)

    # ================================================================= login
    def login_state(self) -> dict:
        try:
            app = a11y.wechat_app()
        except Exception:
            app = None
        if app is None:
            return {"state": "not_running", "logged_in": False, "message": "WeChat client is starting"}
        if self.main_frame() is not None:
            return {"state": "logged_in", "logged_in": True, "message": "已登录"}
        names = set()
        for f in a11y.frames(app):
            for n, d in a11y.walk(f, 30):
                if d > 2 and not a11y.is_showing(n):
                    continue
                nm = a11y.name(n)
                if nm:
                    names.add(nm)
        st = {"logged_in": False, "visible_text": sorted(names)[:30]}
        if "网络连接已断开" in names or "无法加载" in names:
            st.update(state="network_error", message="客户端网络断开，正在重试")
        elif "需在手机上完成登录" in names and ("进入微信" not in names and "登录" not in names):
            st.update(state="waiting_phone_confirm", message="请在手机微信上确认登录")
        elif "进入微信" in names or "登录" in names:
            st.update(state="confirm_needed", message="需要点“进入微信/登录”，然后在手机上确认（服务会自动点）")
        elif any(x in names for x in ("正在进入", "正在加载")):
            st.update(state="loading", message="正在进入微信")
        elif "扫码登录" in names or "二维码" in names or "重新扫码" in names:
            st.update(state="qr", message="请用手机微信扫码登录")
        else:
            st.update(state="unknown", message="未知状态，请查看截图")
        return st

    def _login_frame(self):
        fr = a11y.frames(self.app())
        return fr[0] if fr else None

    def login_screenshot(self, full: bool = False) -> bytes:
        self.calibrate()
        if not full:
            try:
                f = self._login_frame()
                if f is not None and self.main_frame() is None:
                    b = a11y.box(f)
                    if 0 < b.w < 900:
                        x, y = self.screen_xy(f, 0, 0)
                        return xinput.screenshot(crop=(x, y, round(b.w * self.R), round(b.h * self.R)))
            except Exception:
                pass
        return xinput.screenshot(scale=0.75)

    def refresh_qr(self) -> dict:
        self.calibrate()
        for nm in ("重新扫码", "切换账号", "二维码"):
            b = self.shown_one("push button", nm)
            if b is not None:
                self.click(b)
                time.sleep(1.5)
                return {"clicked": nm, **self.login_state()}
        return {"clicked": None, **self.login_state()}

    def calibrate(self):
        """Compute the logical→screen scale from any WeChat frame and its X window (works pre-login)."""
        try:
            for f in a11y.frames(self.app()):
                b = a11y.box(f)
                nm = a11y.name(f) or "微信"
                if b.w <= 0:
                    continue
                for wid in xinput.search_windows("^" + re.escape(nm) + "$"):
                    g = xinput.window_geometry(wid)
                    w = int(g.get("WIDTH", 0))
                    if w > 0 and 0.75 < w / b.w < 3:
                        self.R = w / b.w
                        return self.R
        except Exception:
            pass
        return self.R

    def confirm_login(self) -> dict:
        self.calibrate()
        for nm in ("进入微信", "登录"):
            b = self.shown_one("push button", nm)
            if b is not None:
                self.click(b)
                self.last_prompt_at = time.time()
                time.sleep(1.5)
                self._event(f"clicked {nm} → phone confirmation requested")
                return {"clicked": nm, **self.login_state()}
        return {"clicked": None, **self.login_state()}

    def logout(self) -> dict:
        self.require_login()
        f = self.open_settings("账号与存储")
        b = self.shown_one("push button", "退出登录", root=f)
        if b is None:
            raise WeChatError("退出登录 button not found")
        self.click(b)
        time.sleep(1.0)
        for nm in ("退出", "确定", "退出登录"):
            for p in self.popups():
                c = self.shown_one("push button", nm, root=p)
                if c is not None and c != b:
                    self.click(c)
                    time.sleep(2)
                    return self.login_state()
        return self.login_state()

    def _event(self, msg: str):
        self.events.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": msg})
        self.events = self.events[-50:]
        log.info(msg)

    def watch_tick(self, auto_confirm_every: float = 600):
        """Called periodically by the login watcher thread."""
        st = self.login_state()
        prev = self.last_login_state.get("state")
        if st["state"] != prev:
            self._event(f"login state: {prev} → {st['state']}")
        self.last_login_state = st
        if st["state"] == "confirm_needed":
            # back off so an absent user's phone is not spammed: 10m, 30m, 1.5h, then every 4h
            wait = min(auto_confirm_every * (3 ** self.prompt_count), 4 * 3600)
            if time.time() - self.last_prompt_at > wait:
                self.confirm_login()
                self.prompt_count += 1
        if st["state"] == "logged_in":
            self.prompt_count = 0
            self.fix_window()
            self.dismiss_noise()
            if not self.state.get("settings_applied"):
                try:
                    self.apply_recommended_settings()
                except Exception as e:
                    log.warning("apply settings failed: %s", e)
        return st

    def dismiss_noise(self):
        """Close 'what's new' and similar modal overlays that block the main window."""
        mf = self.main_frame()
        if mf is None:
            return
        for lbl in ("我知道了", "以后再说", "暂不", "关闭提示"):
            b = self.shown_one("push button", lbl)
            if b is not None:
                self.click(b)
                time.sleep(0.4)

    # ============================================================== settings
    def open_settings(self, tab: str = "账号与存储"):
        f = self._settings_frame()
        if f is None:
            more = self.shown_one("push button", "更多", root=self.main_frame())
            if more is None:
                raise WeChatError("更多 button not found")
            self.click(more)
            time.sleep(0.8)
            for p in self.popups():
                s = a11y.find_one(p, role_name="push button", name_eq="设置")
                if s is not None:
                    self.click(s)
                    break
            f = a11y.wait_for(self._settings_frame, 5)
            if f is None:
                raise WeChatError("could not open settings window")
        t = a11y.find_one(f, role_name="push button", name_eq=tab)
        if t is not None:
            self.click(t)
            time.sleep(0.8)
        return self._settings_frame()

    def _settings_frame(self):
        for f in a11y.frames(self.app()):
            if a11y.name(f) in ("设置", "Settings") and xinput.search_windows("^" + a11y.name(f) + "$"):
                return f
        return None

    def close_settings(self):
        for _ in range(3):
            f = self._settings_frame()
            if f is None:
                break
            self.activate_frame(f)
            xinput.key("Escape")
            time.sleep(0.6)
        mf = self.main_frame()
        if mf is not None:
            self.activate_frame(mf)

    def _click_checkbox(self, f, cb):
        """Click a settings checkbox, scrolling the settings page first if it lies below the fold."""
        fb = a11y.box(f)
        for _ in range(6):
            b = a11y.box(cb)
            if b.y + b.h <= fb.y + fb.h - 4 and b.y >= fb.y + 30:
                break
            x, y = self.screen_xy(f, 0.65, 0.6)
            xinput.scroll(x, y, -3 if b.y + b.h > fb.y + fb.h - 4 else 3)
            time.sleep(0.4)
        self.click(cb)
        time.sleep(0.5)

    def get_settings(self) -> dict:
        self.require_login()
        out = {}
        for tab in ("账号与存储", "通用", "通知"):
            f = self.open_settings(tab)
            for cb in a11y.find_all(f, role_name="check box"):
                if a11y.is_showing(cb) and a11y.name(cb):
                    out[a11y.name(cb)] = "checked" in a11y.states(cb)
        self.close_settings()
        return out

    def set_setting(self, name: str, value: bool) -> dict:
        self.require_login()
        for tab in ("账号与存储", "通用", "通知"):
            f = self.open_settings(tab)
            for cb in a11y.find_all(f, role_name="check box", name_eq=name):
                if not a11y.is_showing(cb):
                    continue
                cur = "checked" in a11y.states(cb)
                if cur != value:
                    self._click_checkbox(f, cb)
                new = "checked" in a11y.states(cb)
                self.close_settings()
                return {"setting": name, "before": cur, "after": new}
        self.close_settings()
        raise WeChatError(f"setting {name!r} not found")

    def apply_recommended_settings(self):
        """Turn off notification sounds (known to destabilise headless clients) and enable
        voice-to-text so voice messages are readable as text."""
        want = {"新消息通知声音": False, "语音和视频通话通知声音": False, "聊天中的语音消息自动转成文字": True,
                "保留聊天记录": True}
        done = {}
        for tab in ("账号与存储", "通用", "通知"):
            f = self.open_settings(tab)
            for cb in a11y.find_all(f, role_name="check box"):
                nm = a11y.name(cb)
                if nm in want and a11y.is_showing(cb):
                    if ("checked" in a11y.states(cb)) != want[nm]:
                        self._click_checkbox(f, cb)
                    done[nm] = "checked" in a11y.states(cb)
        self.close_settings()
        self.state["settings_applied"] = True
        _save_state(self.state)
        self._event(f"applied recommended settings: {done}")
        return done

    # ============================================================== profile
    def my_profile(self) -> dict:
        self.require_login()
        self.close_popups()
        nav = a11y.find_one(self.main_frame(), role_name="tool bar", name_in=("导航", "Navigation"))
        nb = a11y.box(nav)
        # own avatar sits at the top of the navigation bar, above the first tab button
        xinput.click(int((nb.x + nb.w / 2) * self.R), int((nb.y + 64) * self.R))
        time.sleep(1.0)
        info = self._read_profile_popup()
        self.close_popups()
        return info

    def _read_profile_popup(self) -> dict:
        cands = [p for p in self.popups() if not a11y.name(p)]
        cands.sort(key=lambda p: 0 if "active" in a11y.states(p) else 1)
        for p in cands:
            labels = [a11y.name(n) for n, d in a11y.walk(p, 30)
                      if a11y.role(n) == "label" and a11y.name(n) and a11y.is_showing(n)]
            buttons = [a11y.name(n) for n, d in a11y.walk(p, 30)
                       if a11y.role(n) == "push button" and a11y.name(n) and a11y.is_showing(n)]
            if not labels:
                continue
            info = {"display_name": labels[0]}
            keymap = {"昵称：": "nickname", "微信号：": "wechat_id", "地区：": "region", "备注": "remark",
                      "标签": "tags", "共同群聊": "common_groups", "来源": "source", "添加时间": "added_on",
                      "个性签名": "signature", "电话": "phone", "描述": "description", "群昵称：": "group_nickname",
                      "朋友权限": "permissions"}
            i = 0
            while i < len(labels):
                k = labels[i]
                if k in keymap and i + 1 < len(labels):
                    info[keymap[k]] = labels[i + 1]
                    i += 2
                    continue
                i += 1
            info["actions"] = [b for b in buttons if b]
            info["raw_labels"] = labels[:40]
            return info
        return {}

    # ================================================================ chats
    def nav(self, tab: str):
        """Switch the left navigation: 微信 / 通讯录 / 收藏 / 朋友圈 / 视频号."""
        self.require_login()
        b = self.shown_one("push button", tab, root=a11y.find_one(self.main_frame(), role_name="tool bar",
                                                                    name_in=("导航", "Navigation")))
        if b is None:
            raise WeChatError(f"navigation tab {tab!r} not found")
        self.click(b)
        time.sleep(0.8)

    def session_list(self):
        n = self._cached("sesslist", self._find_session_list)
        return n

    def _find_session_list(self):
        for l in a11y.find_all(self.main_frame(), role_name="list"):
            if a11y.name(l) in ("会话", "Chats", "折叠的聊天") and a11y.is_showing(l) and a11y.box(l).w > 0:
                return l
        return None

    def ensure_chats_tab(self):
        self.require_login()
        self.close_popups()
        if self.session_list() is None:
            self.nav("微信")
        l = self.session_list()
        if l is not None and a11y.name(l) == "折叠的聊天":
            back = self.shown_one("push button", "返回", root=self.main_frame())
            if back is not None:
                self.click(back)
                time.sleep(0.8)
        l = self.session_list()
        if l is None:
            raise WeChatError("chat list not found")
        return l

    @staticmethod
    def parse_session(name: str) -> dict:
        parts = name.split("\n")
        title = parts[0].strip() if parts else ""
        preview = parts[1].strip() if len(parts) > 1 else ""
        t = parts[2].strip() if len(parts) > 2 else ""
        flags = [p.strip() for p in parts[3:] if p.strip()]
        unread = 0
        m = re.match(r"^\[(\d+)条\]\s*(.*)$", preview)
        if m and not m.group(2).strip() and len(parts) > 2:
            # unmuted chat with unread count: "name\n[3条] \npreview\ntime\n"
            unread = int(m.group(1))
            preview = parts[2].strip()
            t = parts[3].strip() if len(parts) > 3 else ""
            flags = [p.strip() for p in parts[4:] if p.strip()]
        elif m:
            unread = int(m.group(1))
            preview = m.group(2).strip()
        sender = None
        m2 = re.match(r"^([^:：]{1,40})[:：]\s(.*)$", preview)
        if m2:
            sender = m2.group(1)
        d = {"name": title, "last_message": preview, "time": t, "unread": unread,
             "muted": "消息免打扰" in flags}
        if sender:
            d["last_sender"] = sender
        if "[有人@我]" in preview or "[@我]" in preview:
            d["mentioned_me"] = True
        extra = [f for f in flags if f != "消息免打扰"]
        if extra:
            d["flags"] = extra
        return d

    def _visible_items(self, lst) -> list:
        lb = a11y.box(lst)
        out = []
        for it in a11y.children(lst):
            b = a11y.box(it)
            if b.h <= 0:
                continue
            if b.y + b.h <= lb.y + 2 or b.y >= lb.y + lb.h - 2:
                continue
            out.append(it)
        return out

    def _session_unread_badges(self, items) -> dict:
        """Unread counts of non-muted chats are drawn as red badges on the avatar; detect via pixels."""
        if not items:
            return {}
        shot = Shot()
        res = {}
        for it in items:
            b = a11y.box(it)
            # badge sits at the avatar's top-right corner: logical ~ (x+44..x+56, y+8..y+20)
            x0, y0 = int((b.x + 42) * self.R), int((b.y + 6) * self.R)
            red = 0
            tot = 0
            for yy in range(y0, y0 + int(14 * self.R)):
                for xx in range(x0, x0 + int(14 * self.R)):
                    r, g, bl = shot.px(xx, yy)
                    tot += 1
                    if r > 200 and g < 110 and bl < 110:
                        red += 1
            res[a11y.name(it)] = red / max(tot, 1)
        return res

    def list_chats(self, limit: int = 50, folded: bool = False, scan: bool = True) -> list:
        lst = self.ensure_chats_tab()
        if folded:
            fi = next((it for it in a11y.children(lst) if a11y.name(it).startswith("折叠的聊天")), None)
            if fi is None:
                # scroll to find it
                fi = self._scroll_find_session(lambda n: n.startswith("折叠的聊天"))
            if fi is None:
                return []
            self.click(fi)
            time.sleep(1.0)
            lst = self.session_list()
        seen = {}
        order = []
        stale = 0
        lb = a11y.box(lst)
        # go to the top first
        xinput.scroll(int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R), 30)
        time.sleep(0.4)
        while len(order) < limit:
            items = self._visible_items(lst)
            badges = self._session_unread_badges(items)
            new = 0
            for it in items:
                nm = a11y.name(it)
                if nm in seen:
                    continue
                d = self.parse_session(nm)
                if not d["unread"] and badges.get(nm, 0) > 0.25:
                    d["unread"] = -1   # has unread messages, exact count unknown
                    d["unread_badge"] = True
                seen[nm] = d
                order.append(d)
                new += 1
                if len(order) >= limit:
                    break
            if not scan or len(order) >= limit:
                break
            if new == 0:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
            xinput.scroll(int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R), -5)
            time.sleep(0.5)
        # scroll back to top so the list is in a predictable state
        xinput.scroll(int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R), 60)
        time.sleep(0.3)
        if folded:
            back = self.shown_one("push button", "返回", root=self.main_frame())
            if back is not None:
                self.click(back)
                time.sleep(0.6)
        for d in order:
            if d.get("name", "").startswith("折叠的聊天"):
                d["is_folder"] = True
        return order

    def _scroll_find_session(self, pred, max_scrolls: int = 25):
        lst = self.session_list()
        lb = a11y.box(lst)
        cx, cy = int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R)
        xinput.scroll(cx, cy, 40)
        time.sleep(0.3)
        last = None
        for _ in range(max_scrolls):
            items = self._visible_items(lst)
            for it in items:
                if pred(a11y.name(it).split("\n")[0]) or pred(a11y.name(it)):
                    return it
            names = tuple(a11y.name(i) for i in items)
            if names == last:
                break
            last = names
            xinput.scroll(cx, cy, -5)
            time.sleep(0.45)
        return None

    # ------------------------------------------------------------ open chat
    def current_chat(self) -> Optional[dict]:
        mf = self.main_frame()
        if mf is None:
            return None
        lst = self.message_list()
        if lst is None:
            return None
        lb = a11y.box(lst)
        best = None
        root = self.chat_pane() or mf
        for n, d in a11y.walk(root, 40, prune_hidden=True, no_descend=a11y.CONTAINER_ROLES):
            if a11y.role(n) != "label":
                continue
            b = a11y.box(n)
            if b.y < lb.y and b.y > lb.y - 60 and lb.x <= b.x <= lb.x + 40 and a11y.is_showing(n) and a11y.name(n):
                best = n
                break
        if best is None:
            return None
        full = a11y.name(best)
        m = GROUP_TITLE_RE.match(full)
        if m:
            return {"name": m.group(1).strip(), "is_group": True, "member_count": int(m.group(2)), "title": full}
        return {"name": full, "is_group": False, "title": full}

    def _cached(self, key, finder):
        n = getattr(self, "_c_" + key, None)
        if n is not None:
            try:
                if a11y.is_showing(n) and a11y.box(n).w > 0:
                    return n
            except Exception:
                pass
        n = finder()
        setattr(self, "_c_" + key, n)
        return n

    def message_list(self):
        return self._cached("msglist", self._find_message_list)

    def chat_pane(self):
        """Smallest ancestor of the message list that also contains the chat header (title) row."""
        lst = self.message_list()
        if lst is None:
            return None
        lb = a11y.box(lst)
        n = lst
        for _ in range(12):
            try:
                n = n.parent
            except Exception:
                return None
            if n is None:
                return None
            b = a11y.box(n)
            if b.y <= lb.y - 40 and b.x <= lb.x + 2:
                return n
        return None

    def _find_message_list(self):
        for l in a11y.find_all(self.main_frame(), role_name="list"):
            if a11y.name(l) in ("消息", "Messages") and a11y.is_showing(l) and a11y.box(l).w > 0:
                return l
        return None

    def input_box(self):
        return self._cached("input", self._find_input_box)

    def _find_input_box(self):
        lst = self.message_list()
        if lst is None:
            return None
        lb = a11y.box(lst)
        for n in a11y.find_all(self.chat_pane() or self.main_frame(), role_name="text"):
            b = a11y.box(n)
            if b.y >= lb.y + lb.h - 10 and a11y.is_showing(n) and "editable" in a11y.states(n):
                return n
        return None

    def open_chat(self, name: str, allow_search: bool = True) -> dict:
        """Open a chat by exact display name (remark / nickname / group name)."""
        name = name.strip()
        self.require_login()
        cur = self.current_chat()
        if cur and cur["name"] == name and self.session_list() is not None:
            return cur
        lst = self.ensure_chats_tab()
        target = None
        for it in self._visible_items(lst):
            if a11y.name(it).split("\n")[0].strip() == name:
                target = it
                break
        if target is not None:
            self.click(target)
            ok = a11y.wait_for(lambda: (self.current_chat() or {}).get("name") == name, 4)
            if ok:
                return self.current_chat()
        if not allow_search:
            raise WeChatError(f"chat {name!r} not found in the chat list")
        res = self.search(name, open_first_exact=True)
        cur = self.current_chat()
        if cur and cur["name"] == name:
            return cur
        cands = [r["name"] for r in res.get("results", []) if r.get("section") in ("联系人", "群聊", "功能", "最常使用")]
        raise WeChatError(f"chat {name!r} not found. Search suggestions: {cands[:10]}")

    # --------------------------------------------------------------- search
    def _search_box(self):
        mf = self.main_frame()
        for n in a11y.find_all(mf, role_name="text", name_in=("搜索", "Search")):
            b = a11y.box(n)
            if a11y.is_showing(n) and b.x < 320 and b.y < 80:
                return n
        return None

    def search(self, query: str, open_first_exact: bool = False, open_index: Optional[int] = None) -> dict:
        """Global search box: contacts, groups, chat history, functions."""
        self.ensure_chats_tab()
        sb = self._search_box()
        if sb is None:
            raise WeChatError("search box not found")
        self.click(sb)
        time.sleep(0.3)
        xinput.key("ctrl+a")
        xinput.set_clipboard_text(query)
        xinput.key("ctrl+v")
        time.sleep(1.6)
        results = []
        items = []
        popup = None
        for p in self.popups():
            its = [n for n in a11y.find_all(p, role_name="list item") if a11y.box(n).w > 0]
            if its:
                popup = p
                items = its
                break
        section = None
        for it in items:
            nm = a11y.name(it)
            b = a11y.box(it)
            if b.h <= 34 and "\n" not in nm:
                section = nm
                continue
            parts = nm.split("\n")
            results.append({"name": parts[0], "detail": " | ".join(p for p in parts[1:] if p), "section": section})
        out = {"query": query, "results": results}
        chosen = None
        if open_first_exact:
            for it, r in zip([i for i in items if not (a11y.box(i).h <= 34 and "\n" not in a11y.name(i))], results):
                if r["name"] == query and r["section"] not in ("聊天记录", "搜索网络结果", "搜一搜"):
                    chosen = it
                    break
        elif open_index is not None:
            real = [i for i in items if not (a11y.box(i).h <= 34 and "\n" not in a11y.name(i))]
            if 0 <= open_index < len(real):
                chosen = real[open_index]
        if chosen is not None:
            self.click(chosen)
            time.sleep(1.2)
            out["opened"] = a11y.name(chosen).split("\n")[0]
        else:
            xinput.key("Escape")
            time.sleep(0.3)
            xinput.key("Escape")
            time.sleep(0.2)
        return out

    # ============================================================= messages
    def _classify_items(self, lst, chat: dict, resolve_senders: bool, max_lookups: int,
                        lookups_done: list) -> list:
        """Turn the visible message list items into dicts (kind, direction, sender, text)."""
        lb = a11y.box(lst)
        items = self._visible_items(lst)
        if not items:
            return []
        shot = Shot()
        R = self.R
        left_x = int((lb.x + 20) * R)
        right_x = int((lb.x + lb.w - 20 - 36) * R)
        size = int(36 * R)
        out = []
        for it in items:
            b = a11y.box(it)
            if b.y < lb.y - 1 or b.y + b.h > lb.y + lb.h + 1:
                continue   # clipped at the top/bottom edge: classified on a later pass
            text = a11y.name(it)
            top = int(b.y * R)
            entry = {"text": text, "_y": b.y, "_h": b.h}
            if TIME_RE.match(text.strip()) and b.h <= 48:
                entry["kind"] = "time"
                out.append(entry)
                continue
            ay = top + int(8 * R)
            lbusy = shot.busy(left_x + 2, ay + 2, size - 4, size - 4, bg=shot.px(left_x - 8, ay + size // 2))
            rbusy = shot.busy(right_x + 2, ay + 2, size - 4, size - 4, bg=shot.px(right_x + size + 8, ay + size // 2))
            if lbusy < 0.2 and rbusy < 0.2:
                entry["kind"] = "system"
                out.append(entry)
                continue
            entry["kind"] = "message"
            mine = rbusy > lbusy
            entry["direction"] = "out" if mine else "in"
            if mine:
                entry["sender"] = "我"
            elif not chat.get("is_group"):
                entry["sender"] = chat.get("name")
            else:
                h = shot.ahash(left_x + 2, ay + 2, size - 4, size - 4)
                entry["_avatar"] = (h, left_x + size // 2, ay + size // 2)
                cache = self.avatar_cache.setdefault(chat["name"], [])
                name = next((n for (hh, n) in cache if hamming(hh, h) <= 6), None)
                if name:
                    entry["sender"] = name
            entry["type"] = self._msg_type(text)
            out.append(entry)
        # resolve unknown group senders by clicking their avatar (exact name from the profile card)
        if chat.get("is_group") and resolve_senders:
            for e in out:
                if e.get("kind") == "message" and e.get("direction") == "in" and not e.get("sender"):
                    if len(lookups_done) >= max_lookups:
                        break
                    h, cx, cy = e["_avatar"]
                    cache = self.avatar_cache.setdefault(chat["name"], [])
                    name = next((n for (hh, n) in cache if hamming(hh, h) <= 6), None)
                    if not name:
                        if e["_y"] < lb.y:   # avatar scrolled out of view
                            continue
                        xinput.click(cx, cy)
                        time.sleep(0.9)
                        info = self._read_profile_popup()
                        xinput.key("Escape")
                        time.sleep(0.35)
                        lookups_done.append(1)
                        name = info.get("display_name")
                        if name:
                            cache.append((h, name))
                    if name:
                        e["sender"] = name
        return out

    @staticmethod
    def _msg_type(text: str) -> str:
        t = text.strip()
        if t in ("图片", "[图片]", "Image", "[Photo]"):
            return "image"
        if t in ("视频", "[视频]", "Video") or t.startswith("视频 "):
            return "video"
        if t.startswith("[语音]") or t.startswith("语音") or re.match(r'^\d+"$', t):
            return "voice"
        if t.startswith("文件") or t.startswith("[文件]"):
            return "file"
        if t.startswith("[链接]") or t.startswith("链接"):
            return "link"
        if t.startswith("[聊天记录]") or t.startswith("聊天记录"):
            return "chat_record"
        if t.startswith("[小程序]") or t.startswith("小程序"):
            return "mini_program"
        if t.startswith("[位置]") or t.startswith("位置"):
            return "location"
        if t.startswith("[名片]") or t.startswith("名片"):
            return "contact_card"
        if t.startswith("[转账]") or t.startswith("转账") or t.startswith("[红包]") or t.startswith("红包"):
            return "money"
        if re.fullmatch(r"(\[[^\[\]]{1,12}\])+", t):
            return "emoji"
        if t in ("动画表情", "[动画表情]", "表情"):
            return "sticker"
        return "text"

    @staticmethod
    def _key(e: dict) -> tuple:
        return (e.get("kind"), e.get("text"), e.get("direction"), round(e.get("_h", 0)))

    def read_messages(self, chat: Optional[str] = None, limit: int = 30, resolve_senders: bool = True,
                      max_sender_lookups: int = 12, max_scrolls: int = 40) -> dict:
        """Read the last `limit` messages of a chat (scrolling back as needed)."""
        if chat:
            info = self.open_chat(chat)
        else:
            self.require_login()
            info = self.current_chat()
            if info is None:
                raise WeChatError("no chat is open; pass chat=<name>")
        lst = self.message_list()
        if lst is None:
            raise WeChatError("message list not found")
        self.close_popups()
        lb = a11y.box(lst)
        cx, cy = int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R)
        # jump to the newest message first
        xinput.scroll(cx, cy, -60)
        time.sleep(0.5)
        lookups: list = []
        acc = self._classify_items(lst, info, resolve_senders, max_sender_lookups, lookups)
        reached_top = False
        scrolls = 0

        def count_msgs(a):
            return sum(1 for e in a if e.get("kind") in ("message", "system"))

        while count_msgs(acc) < limit and scrolls < max_scrolls:
            before_first = [self._key(e) for e in acc[:3]]
            xinput.scroll(cx, cy, 4)
            time.sleep(0.6)
            scrolls += 1
            batch = self._classify_items(lst, info, resolve_senders, max_sender_lookups, lookups)
            if not batch:
                break
            # merge: find the longest suffix of batch that equals a prefix of acc
            bk = [self._key(e) for e in batch]
            ak = [self._key(e) for e in acc]
            k = 0
            for cand in range(min(len(bk), len(ak)), 0, -1):
                if bk[-cand:] == ak[:cand]:
                    k = cand
                    break
            if k == len(bk):           # nothing new appeared → top of history reached
                reached_top = True
                break
            acc = batch[:len(batch) - k] + acc
            if [self._key(e) for e in acc[:3]] == before_first:
                reached_top = True
                break
        # assign timestamps from time separators
        cur_time = None
        msgs = []
        for e in acc:
            if e.get("kind") == "time":
                cur_time = e["text"]
                continue
            if e.get("kind") == "unknown":
                continue
            m = {k: v for k, v in e.items() if not k.startswith("_")}
            if cur_time:
                m["time"] = cur_time
            msgs.append(m)
        msgs = msgs[-limit:]
        for i, m in enumerate(msgs):
            m["index"] = i - len(msgs)    # -1 = newest
        # leave the view at the newest message
        xinput.scroll(cx, cy, -80)
        time.sleep(0.2)
        return {"chat": info, "count": len(msgs), "reached_start_of_local_history": reached_top,
                "messages": msgs,
                "note": ("The Linux client only holds messages received since it logged in (plus what was "
                         "imported from the phone). Older history: wechat_import_history_from_phone.")
                if reached_top else None}

    # ---------------------------------------------------------------- send
    def _focus_input(self):
        inp = self.input_box()
        if inp is None:
            raise WeChatError("message input box not found (is a chat open?)")
        self.click(inp, fx=0.5, fy=0.5)
        time.sleep(0.2)
        return inp

    def _last_message_text(self) -> Optional[str]:
        lst = self.message_list()
        if lst is None:
            return None
        items = [i for i in a11y.children(lst) if a11y.box(i).h > 0]
        return a11y.name(items[-1]) if items else None

    def _type_mention(self, member: str) -> bool:
        xinput.type_ascii("@")
        time.sleep(0.5)
        xinput.set_clipboard_text(member)
        xinput.key("ctrl+v")
        time.sleep(0.9)
        # the member picker popup filters by what was typed; Return picks the highlighted entry
        picker = None
        for p in self.popups():
            its = [n for n in a11y.find_all(p, role_name="list item") if a11y.box(n).w > 0]
            if its:
                picker = its
                break
        if picker:
            exact = next((n for n in picker if a11y.name(n).split("\n")[0] == member), None)
            if exact is not None:
                self.click(exact)
            else:
                xinput.key("Return")
            time.sleep(0.4)
            return True
        return False

    def send_text(self, chat: str, text: str, mention: Optional[list] = None, mention_all: bool = False,
                  verify: bool = True) -> dict:
        info = self.open_chat(chat)
        self.close_popups()
        inp = self._focus_input()
        xinput.key("ctrl+a")
        xinput.key("Delete")
        mentioned = []
        if (mention or mention_all) and info.get("is_group"):
            for m in (["所有人"] if mention_all else []) + list(mention or []):
                if self._type_mention(m):
                    mentioned.append(m)
            xinput.type_ascii(" ")
        xinput.set_clipboard_text(text)
        xinput.key("ctrl+v")
        time.sleep(0.35)
        xinput.key("Return")
        ok = None
        if verify:
            ok = bool(a11y.wait_for(lambda: (self._last_message_text() or "").strip().endswith(text.strip()[-30:]), 5))
        return {"chat": info["name"], "sent": True, "verified": ok, "mentioned": mentioned, "text": text}

    def _materialize(self, path: Optional[str], url: Optional[str], data_b64: Optional[str],
                     filename: Optional[str]) -> str:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        if path:
            p = Path(path)
            if not p.exists():
                raise WeChatError(f"file not found on the server: {path}")
            return str(p)
        if url:
            fn = filename or Path(urllib.request.urlparse(url).path).name or "file"
            dest = OUTBOX / f"{int(time.time())}_{fn}"
            req = urllib.request.Request(url, headers={"User-Agent": "wechat-mcp/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return str(dest)
        if data_b64:
            fn = filename or "file.bin"
            dest = OUTBOX / f"{int(time.time())}_{fn}"
            dest.write_bytes(base64.b64decode(data_b64))
            return str(dest)
        raise WeChatError("provide one of path, url or data_base64")

    def send_files(self, chat: str, paths: list, caption: Optional[str] = None) -> dict:
        info = self.open_chat(chat)
        self.close_popups()
        self._focus_input()
        xinput.key("ctrl+a")
        xinput.key("Delete")
        before = self._last_message_text()
        xinput.set_clipboard_files(paths)
        xinput.key("ctrl+v")
        time.sleep(1.0)
        # WeChat may show a confirmation dialog for multiple files
        for p in self.popups():
            b = self.shown_one("push button", "发送", root=p) or self.shown_one("push button", "发送(S)", root=p)
            if b is not None:
                self.click(b)
                time.sleep(1.0)
                break
        else:
            xinput.key("Return")
        time.sleep(1.5)
        if caption:
            self._focus_input()
            xinput.set_clipboard_text(caption)
            xinput.key("ctrl+v")
            time.sleep(0.3)
            xinput.key("Return")
        changed = a11y.wait_for(lambda: self._last_message_text() != before, 8)
        return {"chat": info["name"], "sent": True, "verified": bool(changed),
                "files": [Path(p).name for p in paths], "last_message": self._last_message_text()}

    # ------------------------------------------------------- message actions
    def _find_message_item(self, text: Optional[str], index: Optional[int], max_scrolls: int = 15):
        lst = self.message_list()
        if lst is None:
            raise WeChatError("message list not found")
        lb = a11y.box(lst)
        cx, cy = int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R)
        xinput.scroll(cx, cy, -60)
        time.sleep(0.4)
        if index is not None and index < 0:
            seen_msgs = []
            for _ in range(max_scrolls):
                items = [i for i in self._visible_items(lst)
                         if not (TIME_RE.match(a11y.name(i).strip()) and a11y.box(i).h <= 48)]
                if len(items) >= -index:
                    return items[index]
                xinput.scroll(cx, cy, 4)
                time.sleep(0.5)
                seen_msgs = items
            raise WeChatError(f"message index {index} not reachable")
        if text is None:
            raise WeChatError("pass message_text (exact or substring) or index (negative, -1 = newest)")
        for _ in range(max_scrolls):
            items = [i for i in self._visible_items(lst)
                     if a11y.box(i).y >= lb.y and a11y.box(i).y + a11y.box(i).h <= lb.y + lb.h]
            for matcher in (lambda nm: nm == text, lambda nm: nm.split("\n")[0] == text,
                            lambda nm: nm.startswith(text), lambda nm: text in nm):
                for it in reversed(items):
                    if matcher(a11y.name(it)):
                        return it
            xinput.scroll(cx, cy, 4)
            time.sleep(0.5)
        raise WeChatError(f"message containing {text!r} not found in the visible history")

    def _bubble_xy(self, item) -> tuple:
        """Screen point inside the message bubble (not the empty row area)."""
        lst = self.message_list()
        lb = a11y.box(lst)
        b = a11y.box(item)
        shot = Shot()
        R = self.R
        left_x = int((lb.x + 20) * R)
        right_x = int((lb.x + lb.w - 20 - 36) * R)
        size = int(36 * R)
        ay = int((b.y + 8) * R)
        lbusy = shot.busy(left_x + 2, ay + 2, size - 4, size - 4)
        rbusy = shot.busy(right_x + 2, ay + 2, size - 4, size - 4)
        mine = rbusy > lbusy
        chat = self.current_chat() or {}
        # first text line of the bubble: below the sender-name strip for incoming group messages
        off = 50 if (chat.get("is_group") and not mine) else 28
        y = int((b.y + min(off, max(12, b.h - 12))) * R)
        if mine:
            return right_x - int(26 * R), y
        return left_x + size + int(26 * R), y

    def message_menu(self, chat: Optional[str], text: Optional[str], index: Optional[int]) -> tuple:
        if chat:
            self.open_chat(chat)
        else:
            self.require_login()
        self.close_popups()
        it = self._find_message_item(text, index)
        x, y = self._bubble_xy(it)
        xinput.right_click(x, y)
        time.sleep(0.8)
        for p in self.popups():
            items = [n for n in a11y.find_all(p, role_name="menu item") if a11y.box(n).w > 0]
            if items:
                return it, items
        xinput.key("Escape")
        raise WeChatError("context menu did not open for that message")

    def message_action(self, chat: Optional[str], action: str, text: Optional[str] = None,
                       index: Optional[int] = None) -> dict:
        it, items = self.message_menu(chat, text, index)
        names = [a11y.name(n) for n in items]
        target = next((n for n in items if a11y.name(n) == action), None) or \
            next((n for n in items if a11y.name(n).startswith(action)), None)
        if target is None:
            xinput.key("Escape")
            raise WeChatError(f"action {action!r} not available for this message; available: {names}")
        act_name = a11y.name(target)
        msg_name = a11y.name(it)
        self.click(target)
        time.sleep(0.8)
        return {"message": msg_name, "action": act_name, "menu": names}

    def message_menu_options(self, chat: Optional[str], text: Optional[str], index: Optional[int]) -> dict:
        it, items = self.message_menu(chat, text, index)
        names = [a11y.name(n) for n in items]
        xinput.key("Escape")
        return {"message": a11y.name(it), "options": names}

    def _confirm_dialog(self, labels=("确定", "删除", "撤回", "发送", "好的", "确认", "退出")) -> Optional[str]:
        for p in self.popups():
            for lbl in labels:
                b = self.shown_one("push button", lbl, root=p)
                if b is not None:
                    self.click(b)
                    time.sleep(0.8)
                    return lbl
        return None

    def reply(self, chat: str, text: str, quote_text: Optional[str] = None, quote_index: Optional[int] = None) -> dict:
        self.open_chat(chat)
        r = self.message_action(None, "引用", quote_text, quote_index)
        self._focus_input()
        xinput.key("End")
        xinput.set_clipboard_text(text)
        xinput.key("ctrl+v")
        time.sleep(0.3)
        xinput.key("Return")
        ok = a11y.wait_for(lambda: text.strip()[-20:] in (self._last_message_text() or ""), 5)
        return {"chat": chat, "quoted": r["message"], "text": text, "verified": bool(ok)}

    def recall(self, chat: str, text: Optional[str] = None, index: Optional[int] = None) -> dict:
        r = self.message_action(chat, "撤回", text, index)
        c = self._confirm_dialog(("撤回", "确定"))
        return {**r, "confirmed": c}

    def delete_message(self, chat: str, text: Optional[str] = None, index: Optional[int] = None) -> dict:
        r = self.message_action(chat, "删除", text, index)
        c = self._confirm_dialog(("删除", "确定"))
        return {**r, "confirmed": c}

    def copy_message(self, chat: Optional[str], text: Optional[str] = None, index: Optional[int] = None) -> dict:
        r = self.message_action(chat, "复制", text, index)
        time.sleep(0.3)
        return {**r, "clipboard": xinput.get_clipboard_text()}

    def forward(self, chat: str, to: list, text: Optional[str] = None, index: Optional[int] = None,
                comment: Optional[str] = None) -> dict:
        r = self.message_action(chat, "转发", text, index)
        time.sleep(1.0)
        dlg = None
        for p in self.popups():
            if self.shown("text", root=p):
                dlg = p
                break
        if dlg is None:
            raise WeChatError("forward dialog did not open")
        picked = []
        for name in to:
            sb = self.shown("text", root=dlg)[0]
            self.click(sb)
            xinput.key("ctrl+a")
            xinput.set_clipboard_text(name)
            xinput.key("ctrl+v")
            time.sleep(1.2)
            cands = self._picker_candidates(dlg)
            hit = next((n for n in cands if a11y.name(n).split("\n")[0] == name), None)
            if hit is None:
                hit = next((n for n in cands if name in a11y.name(n)), None)
            if hit is not None:
                self.click(hit)
                picked.append(a11y.name(hit).split("\n")[0])
                time.sleep(0.4)
        if not picked:
            xinput.key("Escape")
            raise WeChatError(f"none of the forward targets were found: {to}")
        if comment:
            texts = self.shown("text", root=dlg)
            if len(texts) > 1:
                self.click(texts[-1])
                xinput.set_clipboard_text(comment)
                xinput.key("ctrl+v")
        sent = None
        for lbl in ("发送", "分别发送", "发送(S)", "确定"):
            b = self.shown_one("push button", lbl, root=dlg)
            if b is None:
                b = next((x for x in self.shown("push button", root=dlg) if a11y.name(x).startswith(lbl)), None)
            if b is not None:
                sent = a11y.name(b) or lbl
                self.click(b)
                time.sleep(1.0)
                break
        return {**r, "forwarded_to": picked, "button": sent}

    def _picker_candidates(self, dlg) -> list:
        """Selectable rows of a contact picker dialog (forward / create group): check boxes or list items."""
        return [n for n in a11y.find_all(dlg, pred=lambda x: a11y.role(x) in ("check box", "list item"))
                if a11y.is_showing(n) and a11y.box(n).w > 0 and a11y.name(n)]

    def view_message(self, chat: Optional[str], text: Optional[str] = None, index: Optional[int] = None) -> bytes:
        if chat:
            self.open_chat(chat)
        else:
            self.require_login()
        self.close_popups()
        it = self._find_message_item(text, index)
        lst = self.message_list()
        lb = a11y.box(lst)
        b = a11y.box(it)
        y0 = max(b.y, lb.y)
        y1 = min(b.y + b.h, lb.y + lb.h)
        return xinput.screenshot(crop=(int(lb.x * self.R), int(y0 * self.R), int(lb.w * self.R),
                                       max(10, int((y1 - y0) * self.R))))

    def chat_screenshot(self, chat: Optional[str] = None) -> bytes:
        if chat:
            self.open_chat(chat)
        else:
            self.require_login()
        lst = self.message_list()
        if lst is None:
            return xinput.screenshot(scale=0.75)
        lb = a11y.box(lst)
        return xinput.screenshot(crop=(int(lb.x * self.R), int((lb.y - 48) * self.R), int(lb.w * self.R),
                                       int((lb.h + 48) * self.R)))

    def open_image(self, chat: Optional[str], text: Optional[str] = None, index: Optional[int] = None) -> bytes:
        """Open an image message in WeChat's viewer and return a screenshot of it (full resolution view)."""
        if chat:
            self.open_chat(chat)
        it = self._find_message_item(text or "图片", index)
        x, y = self._bubble_xy(it)
        xinput.click(x, y)
        time.sleep(2.0)
        data = xinput.screenshot()
        xinput.key("Escape")
        time.sleep(0.5)
        return data

    # ------------------------------------------------------ session actions
    def session_menu(self, chat: str):
        lst = self.ensure_chats_tab()
        it = next((i for i in self._visible_items(lst) if a11y.name(i).split("\n")[0].strip() == chat), None)
        if it is None:
            it = self._scroll_find_session(lambda n: n.strip() == chat)
        if it is None:
            raise WeChatError(f"chat {chat!r} is not in the chat list (it may be in 折叠的聊天 or hidden)")
        self.click(it, button=3)
        time.sleep(0.8)
        for p in self.popups():
            items = [n for n in a11y.find_all(p, role_name="menu item") if a11y.box(n).w > 0]
            if items:
                return it, items
        xinput.key("Escape")
        raise WeChatError("chat context menu did not open")

    def session_action(self, chat: str, action: str, confirm: bool = True) -> dict:
        it, items = self.session_menu(chat)
        names = [a11y.name(n) for n in items]
        target = next((n for n in items if a11y.name(n) == action), None)
        if target is None:
            xinput.key("Escape")
            raise WeChatError(f"{action!r} not available for {chat!r}; available: {names}")
        self.click(target)
        time.sleep(0.8)
        c = self._confirm_dialog(("删除", "确定", "不显示")) if confirm else None
        return {"chat": chat, "action": action, "menu_before": names, "confirmed": c}

    def session_options(self, chat: str) -> dict:
        it, items = self.session_menu(chat)
        names = [a11y.name(n) for n in items]
        xinput.key("Escape")
        return {"chat": chat, "options": names}

    # ============================================================ chat info
    def open_chat_info(self, chat: str):
        info = self.open_chat(chat)
        self.close_popups()
        if not self._info_panel_open():
            b = self.shown_one("push button", "聊天信息", root=self.main_frame())
            if b is None:
                raise WeChatError("聊天信息 button not found (this chat has no info panel)")
            self.click(b)
            a11y.wait_for(self._info_panel_open, 4)
        panel = self._members_list()
        if panel is None and not self._info_panel_open():
            raise WeChatError("chat info panel did not open")
        return info, panel

    def _info_panel_open(self) -> bool:
        if self._members_list() is not None:
            return True
        return self.shown_one("push button", "查找聊天内容", root=self.chat_pane() or self.main_frame()) is not None

    def _dm_avatar_button(self, name: str):
        lst = self.message_list()
        lb = a11y.box(lst) if lst is not None else None
        for n in self.shown("push button", name, root=self.chat_pane() or self.main_frame()):
            if lb is None or a11y.box(n).x > lb.x + lb.w * 0.6:
                return n
        return None

    def _members_list(self):
        for l in a11y.find_all(self.main_frame(), role_name="list", name_in=("聊天成员", "Members")):
            if a11y.is_showing(l) and a11y.box(l).w > 0:
                return l
        return None

    def close_chat_info(self):
        if self._info_panel_open():
            b = self.shown_one("push button", "聊天信息", root=self.main_frame())
            if b is not None:
                self.click(b)
                time.sleep(0.5)

    def group_members(self, chat: str, limit: int = 500) -> dict:
        info, panel = self.open_chat_info(chat)
        pb = a11y.box(panel)
        cx, cy = int((pb.x + pb.w / 2) * self.R), int((pb.y + pb.h / 2) * self.R)

        def names():
            return [a11y.name(i) for i in a11y.children(panel) if a11y.name(i) and a11y.name(i) not in ("添加", "移出", "删除")]

        before = names()
        # expand "查看更多" (drawn just below the last member row; not exposed to AT-SPI)
        expected = info.get("member_count") or 0
        if expected and len(before) < expected:
            kids = [i for i in a11y.children(panel) if a11y.box(i).h > 0]
            if kids:
                last = a11y.box(kids[-1])
                xinput.click(int((pb.x + pb.w / 2) * self.R), int((last.y + last.h + 14) * self.R))
                time.sleep(1.2)
        members = []
        seen = set()
        stale = 0
        for _ in range(60):
            for n in names():
                if n not in seen:
                    seen.add(n)
                    members.append(n)
            if len(members) >= min(limit, expected or 10**6):
                break
            xinput.scroll(cx, cy, -5)
            time.sleep(0.5)
            if len(set(names()) - seen) == 0:
                stale += 1
                if stale >= 3:
                    break
            else:
                stale = 0
        self.close_chat_info()
        return {"chat": info["name"], "is_group": info.get("is_group"), "member_count": expected or len(members),
                "members": members[:limit],
                "complete": (len(members) >= expected) if expected else None}

    def contact_info(self, name: str) -> dict:
        """Profile card of a contact (opens their chat → 聊天信息 → avatar)."""
        info, panel = self.open_chat_info(name)
        if info.get("is_group"):
            self.close_chat_info()
            raise WeChatError(f"{name!r} is a group; use wechat_group_info / wechat_group_members")
        target = self._dm_avatar_button(info["name"])
        if target is None and panel is not None:
            kids = [i for i in a11y.children(panel) if a11y.name(i) and a11y.name(i) not in ("添加",)]
            target = kids[0] if kids else None
        if target is None:
            self.close_chat_info()
            raise WeChatError("no contact avatar in the chat info panel")
        self.click(target)
        time.sleep(1.0)
        prof = self._read_profile_popup()
        xinput.key("Escape")
        time.sleep(0.3)
        self.close_chat_info()
        return prof

    def group_member_info(self, group: str, member: str) -> dict:
        info, panel = self.open_chat_info(group)
        sb = next((t for t in self.shown("text", root=self.main_frame())
                   if a11y.box(t).x >= a11y.box(panel).x - 10), None)
        if sb is not None:
            self.click(sb)
            xinput.key("ctrl+a")
            xinput.set_clipboard_text(member)
            xinput.key("ctrl+v")
            time.sleep(1.0)
        kids = [i for i in a11y.children(panel) if a11y.name(i) == member and a11y.box(i).h > 0] or \
               [i for i in a11y.children(panel) if member in a11y.name(i) and a11y.box(i).h > 0]
        if not kids:
            self.close_chat_info()
            raise WeChatError(f"member {member!r} not found in {group!r}")
        self.click(kids[0])
        time.sleep(1.0)
        prof = self._read_profile_popup()
        xinput.key("Escape")
        time.sleep(0.3)
        if sb is not None:
            self.click(sb)
            xinput.key("ctrl+a")
            xinput.key("Delete")
        self.close_chat_info()
        return prof

    # ============================================================= contacts
    def contacts_list(self):
        for l in a11y.find_all(self.main_frame(), role_name="list"):
            if a11y.is_showing(l) and a11y.box(l).w > 0 and a11y.box(l).x < 320:
                return l
        return None

    def list_contacts(self, limit: int = 2000, include_sections: bool = False) -> dict:
        self.nav("通讯录")
        time.sleep(0.5)
        lst = self.contacts_list()
        if lst is None:
            raise WeChatError("contacts list not found")
        lb = a11y.box(lst)
        cx, cy = int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R)
        xinput.scroll(cx, cy, 60)
        time.sleep(0.4)
        out, sections, seen = [], [], set()
        stale = 0
        while len(out) < limit:
            new = 0
            for it in self._visible_items(lst):
                nm = a11y.name(it)
                if not nm or nm in seen:
                    continue
                seen.add(nm)
                new += 1
                h = a11y.box(it).h
                m = re.match(r"^(新的朋友|群聊|公众号、服务号|企业微信联系人|联系人|标签|服务号|公众号|仅聊天的朋友)(\d*)$", nm)
                if m or h <= 40:
                    sections.append({"section": m.group(1) if m else nm,
                                     "count": int(m.group(2)) if m and m.group(2) else None})
                    continue
                out.append(nm)
            if new == 0:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
            xinput.scroll(cx, cy, -5)
            time.sleep(0.45)
        xinput.scroll(cx, cy, 80)
        self.nav("微信")
        res = {"count": len(out), "contacts": out[:limit], "sections": sections}
        return res

    def contacts_entry(self, label_prefix: str):
        self.nav("通讯录")
        lst = self.contacts_list()
        lb = a11y.box(lst)
        xinput.scroll(int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R), 60)
        time.sleep(0.4)
        it = next((i for i in self._visible_items(lst) if a11y.name(i).startswith(label_prefix)), None)
        if it is None:
            raise WeChatError(f"{label_prefix!r} entry not found in 通讯录")
        self.click(it)
        time.sleep(1.2)
        return it

    def right_pane_text(self) -> list:
        """All visible named nodes in the right-hand pane (x > list)."""
        mf = self.main_frame()
        out = []
        for n, d in a11y.walk(mf, 45):
            b = a11y.box(n)
            if b.x >= 300 and b.w > 0 and a11y.is_showing(n) and a11y.name(n):
                out.append({"role": a11y.role(n), "name": a11y.name(n), "x": b.x, "y": b.y})
        return out

    def contacts_manager(self, limit: int = 3000, query: Optional[str] = None) -> dict:
        """Rows of the 通讯录管理 window: '<nickname> <remark> <tags>' per contact."""
        self.nav("通讯录")
        lst = self.contacts_list()
        top = next((i for i in a11y.children(lst) if a11y.box(i).h > 0), None)
        if top is None:
            raise WeChatError("contacts list empty")
        lb = a11y.box(lst)
        xinput.scroll(int((lb.x + lb.w / 2) * self.R), int((lb.y + lb.h / 2) * self.R), 60)
        time.sleep(0.4)
        top = next((i for i in a11y.children(lst) if a11y.box(i).h > 0), None)
        self.click(top)
        win = a11y.wait_for(lambda: next((p for p in self.popups() if a11y.name(p) == "通讯录管理"), None), 5)
        if win is None:
            raise WeChatError("通讯录管理 window did not open")
        if query:
            t = self.shown_one("text", root=win)
            if t is not None:
                self.click(t)
                xinput.set_clipboard_text(query)
                xinput.key("ctrl+v")
                time.sleep(1.0)
        lists = [l for l in a11y.find_all(win, role_name="list") if a11y.box(l).w > 300]
        rows, seen, stale = [], set(), 0
        if lists:
            tl = lists[0]
            for _ in range(400):
                new = 0
                for it in self._visible_items(tl):
                    nm = a11y.name(it)
                    if nm and nm not in seen:
                        seen.add(nm)
                        rows.append(nm)
                        new += 1
                if len(rows) >= limit:
                    break
                stale = stale + 1 if new == 0 else 0
                if stale >= 2:
                    break
                x, y = self.screen_xy(tl)
                xinput.scroll(x, y, -5)
                time.sleep(0.4)
        filters = [a11y.name(i) for l in a11y.find_all(win, role_name="list") if a11y.box(l).w <= 300
                   for i in a11y.children(l) if a11y.name(i)]
        self.activate_frame(win)
        xinput.key("Escape")
        time.sleep(0.4)
        if next((p for p in self.popups() if a11y.name(p) == "通讯录管理"), None) is not None:
            cb = self.shown_one("push button", "关闭", root=win)
            if cb is not None:
                self.click(cb)
        self.nav("微信")
        return {"count": len(rows), "format": "nickname remark tags (space separated; empty fields leave double spaces)",
                "rows": rows[:limit], "filters": filters}

    def new_friend_requests(self) -> dict:
        self.nav("通讯录")
        lst = self.contacts_list()
        if lst is None or not any(a11y.name(i).startswith("新的朋友") for i in a11y.children(lst)):
            self.nav("微信")
            return {"requests": [], "note": "No pending friend requests (the 新的朋友 entry only appears when there are some)."}
        self.contacts_entry("新的朋友")
        items = []
        for l in a11y.find_all(self.main_frame(), role_name="list"):
            if a11y.is_showing(l) and a11y.box(l).x >= 300:
                for it in a11y.children(l):
                    if a11y.name(it):
                        items.append(a11y.name(it).replace("\n", " | "))
        pane = self.right_pane_text()
        self.nav("微信")
        return {"requests": items, "pane": pane[:80]}

    def accept_friend_request(self, name: str, remark: Optional[str] = None) -> dict:
        self.contacts_entry("新的朋友")
        target = None
        for l in a11y.find_all(self.main_frame(), role_name="list"):
            if a11y.is_showing(l) and a11y.box(l).x >= 300:
                for it in a11y.children(l):
                    if name in a11y.name(it):
                        target = it
        if target is None:
            raise WeChatError(f"no friend request from {name!r}")
        btn = None
        for n, d in a11y.walk(target, 10):
            if a11y.role(n) == "push button" and a11y.name(n) in ("接受", "通过验证", "Accept"):
                btn = n
        self.click(btn or target)
        time.sleep(1.2)
        if remark:
            for p in self.popups():
                t = self.shown("text", root=p)
                if t:
                    self.click(t[0])
                    xinput.key("ctrl+a")
                    xinput.set_clipboard_text(remark)
                    xinput.key("ctrl+v")
        c = self._confirm_dialog(("确定", "完成", "接受", "通过验证"))
        return {"name": name, "clicked": a11y.name(btn) if btn else "item", "confirmed": c}

    def quick_action(self, label: str):
        self.ensure_chats_tab()
        b = self.shown_one("push button", "快捷操作", root=self.main_frame())
        if b is None:
            raise WeChatError("快捷操作 (+) button not found")
        self.click(b)
        time.sleep(0.8)
        for p in self.popups():
            it = next((n for n in a11y.find_all(p, role_name="list item") if a11y.name(n) == label), None)
            if it is not None:
                self.click(it)
                time.sleep(1.2)
                return True
        xinput.key("Escape")
        raise WeChatError(f"quick action {label!r} not found")

    def add_friend(self, query: str, greeting: Optional[str] = None, remark: Optional[str] = None) -> dict:
        self.quick_action("添加朋友")
        dlg = None
        for p in self.popups():
            if self.shown("text", root=p):
                dlg = p
        if dlg is None:
            raise WeChatError("添加朋友 dialog did not open")
        t = self.shown("text", root=dlg)[0]
        self.click(t)
        xinput.set_clipboard_text(query)
        xinput.key("ctrl+v")
        xinput.key("Return")
        time.sleep(2.0)
        labels = [a11y.name(n) for n, d in a11y.walk(dlg, 30) if a11y.name(n) and a11y.is_showing(n)]
        add = None
        for lbl in ("添加到通讯录", "添加", "发消息"):
            add = self.shown_one("push button", lbl, root=dlg)
            if add is not None:
                break
        if add is None or a11y.name(add) == "发消息":
            xinput.key("Escape")
            return {"query": query, "found": bool(add), "already_friend": bool(add), "card": labels[:30]}
        self.click(add)
        time.sleep(1.5)
        for p in self.popups():
            texts = self.shown("text", root=p)
            if texts and greeting:
                self.click(texts[0])
                xinput.key("ctrl+a")
                xinput.set_clipboard_text(greeting)
                xinput.key("ctrl+v")
            if len(texts) > 1 and remark:
                self.click(texts[1])
                xinput.key("ctrl+a")
                xinput.set_clipboard_text(remark)
                xinput.key("ctrl+v")
        c = self._confirm_dialog(("确定", "发送"))
        return {"query": query, "found": True, "card": labels[:30], "request_sent": bool(c)}

    def create_group(self, members: list) -> dict:
        self.quick_action("发起群聊")
        dlg = None
        for p in self.popups():
            if self.shown("text", root=p):
                dlg = p
        if dlg is None:
            raise WeChatError("发起群聊 dialog did not open")
        picked = []
        for m in members:
            t = self.shown("text", root=dlg)[0]
            self.click(t)
            xinput.key("ctrl+a")
            xinput.set_clipboard_text(m)
            xinput.key("ctrl+v")
            time.sleep(1.0)
            cands = self._picker_candidates(dlg)
            hit = next((n for n in cands if a11y.name(n).split("\n")[0] == m), None)
            if hit is not None:
                self.click(hit)
                picked.append(m)
                time.sleep(0.3)
        if len(picked) < 2:
            xinput.key("Escape")
            raise WeChatError(f"need at least 2 members found; found {picked}")
        c = None
        for lbl in ("完成", "确定", "创建"):
            b = self.shown_one("push button", lbl, root=dlg)
            if b is not None:
                self.click(b)
                c = lbl
                time.sleep(2.0)
                break
        return {"members": picked, "confirmed": c, "current_chat": self.current_chat()}

    # ============================================================ favorites / moments
    def list_favorites(self, limit: int = 50) -> dict:
        self.nav("收藏")
        time.sleep(1.0)
        out = []
        for l in a11y.find_all(self.main_frame(), role_name="list"):
            if a11y.is_showing(l) and a11y.box(l).w > 0:
                for it in self._visible_items(l):
                    if a11y.name(it):
                        out.append({"list": a11y.name(l), "item": a11y.name(it).replace("\n", " | ")})
        self.nav("微信")
        return {"items": out[:limit]}

    def moments(self, limit: int = 20, scrolls: int = 3) -> dict:
        self.nav("朋友圈")
        time.sleep(2.0)
        win = None
        for p in self.popups():
            if a11y.name(p) in ("朋友圈", "Moments") or a11y.find_one(p, role_name="list"):
                win = p
        if win is None:
            raise WeChatError("Moments window did not open")
        posts, seen = [], set()
        lst = a11y.find_one(win, role_name="list")
        for _ in range(scrolls + 1):
            for it in (a11y.children(lst) if lst else []):
                nm = a11y.name(it) or a11y.all_text(it, 6)
                if nm and nm not in seen and a11y.box(it).h > 0:
                    seen.add(nm)
                    posts.append(nm)
            if len(posts) >= limit or lst is None:
                break
            x, y = self.screen_xy(lst)
            xinput.scroll(x, y, -6)
            time.sleep(1.0)
        # close the Moments window
        cb = self.shown_one("push button", "关闭", root=win)
        if cb is not None:
            self.click(cb)
        else:
            xinput.key("Escape")
        return {"posts": posts[:limit]}

    def import_history_from_phone(self, chat: Optional[str] = None) -> dict:
        if chat:
            self.open_chat(chat)
        b = self.shown_one("push button", "从手机导入聊天记录", root=self.main_frame())
        if b is None:
            raise WeChatError("从手机导入聊天记录 button not found (open a chat first)")
        self.click(b)
        time.sleep(1.5)
        txt = [a11y.name(n) for p in self.popups() for n, d in a11y.walk(p, 20) if a11y.name(n) and a11y.is_showing(n)]
        return {"opened": True, "dialog": txt[:30],
                "hint": "Follow the prompt on the phone (WeChat → 设置 → 聊天 → 聊天记录迁移与备份) to push history to this device."}

    def search_chat_history(self, chat: str, keyword: str) -> dict:
        self.open_chat(chat)
        b = self.shown_one("push button", "聊天记录", root=self.main_frame())
        if b is None:
            raise WeChatError("聊天记录 button not found")
        self.click(b)
        time.sleep(1.5)
        win = next((p for p in self.popups() if self.shown("text", root=p)), None)
        if win is None:
            raise WeChatError("chat history window did not open")
        t = self.shown("text", root=win)[0]
        self.click(t)
        xinput.set_clipboard_text(keyword)
        xinput.key("ctrl+v")
        xinput.key("Return")
        time.sleep(1.5)
        results = []
        for l in a11y.find_all(win, role_name="list"):
            for it in a11y.children(l):
                if a11y.name(it) and a11y.box(it).h > 0:
                    results.append(a11y.name(it).replace("\n", " | "))
        cb = self.shown_one("push button", "关闭", root=win)
        if cb is not None:
            self.click(cb)
        else:
            xinput.key("Escape")
        return {"chat": chat, "keyword": keyword, "results": results}

    # ============================================================ low level
    def ui_tree(self, only_showing: bool = True, max_nodes: int = 1500, popups_only: bool = False) -> str:
        app = self.app()
        if popups_only:
            return "\n\n".join(a11y.dump(p, only_showing=only_showing, max_nodes=max_nodes) for p in self.popups())
        return a11y.dump(app, only_showing=only_showing, max_nodes=max_nodes)

    def click_element(self, name: str, role: Optional[str] = None, index: int = 0, button: int = 1,
                      double: bool = False) -> dict:
        nodes = [n for n in a11y.find_all(self.app(), role_name=role, name_eq=name)
                 if a11y.is_showing(n) and a11y.box(n).w > 0]
        if not nodes:
            nodes = [n for n in a11y.find_all(self.app(), role_name=role, pred=lambda x: name in a11y.name(x))
                     if a11y.is_showing(n) and a11y.box(n).w > 0]
        if len(nodes) <= index:
            raise WeChatError(f"no visible element named {name!r}" + (f" with role {role}" if role else ""))
        n = nodes[index]
        self.click(n, button=button, double=double)
        time.sleep(0.6)
        return {"clicked": a11y.name(n), "role": a11y.role(n), "matches": len(nodes)}


WX = WeChat()
