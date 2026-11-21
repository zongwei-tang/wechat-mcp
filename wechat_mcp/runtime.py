"""Process supervisor: virtual X display, D-Bus session, AT-SPI bus, window manager and WeChat.

Runs inside the container as the unprivileged `wx` user. HOME points at the
persistent volume so WeChat's login state survives redeploys.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("wechat_mcp.runtime")

DISPLAY = os.environ.get("DISPLAY", ":99")
SCREEN = os.environ.get("WECHAT_SCREEN", "1440x900x24")
WECHAT_BIN = os.environ.get("WECHAT_BIN", "/opt/wechat/wechat")


class Runtime:
    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.env = dict(os.environ)
        self.env["DISPLAY"] = DISPLAY
        self.env["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
        self.env["QT_ACCESSIBILITY"] = "1"
        self.env.setdefault("LANG", "zh_CN.UTF-8")
        self.env.setdefault("LC_ALL", "zh_CN.UTF-8")
        self._stop = threading.Event()
        self.wechat_restarts = 0
        self.wechat_started_at = 0.0
        self.paused = False  # set by logout/restart tools to hold the watchdog
        self.logdir = Path(os.environ.get("WECHAT_MCP_HOME", "/data")) / "logs"
        self.logdir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- helpers
    def _spawn(self, key: str, cmd: list, extra_env: dict | None = None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        out = open(self.logdir / f"{key}.log", "ab", buffering=0)
        p = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs[key] = p
        log.info("started %s pid=%s", key, p.pid)
        return p

    def alive(self, key: str) -> bool:
        p = self.procs.get(key)
        return p is not None and p.poll() is None

    # ------------------------------------------------------------------ start
    def start_display(self):
        lock = Path(f"/tmp/.X{DISPLAY.lstrip(':')}-lock")
        if lock.exists():
            lock.unlink(missing_ok=True)
        self._spawn("xvfb", ["Xvfb", DISPLAY, "-screen", "0", SCREEN, "-nolisten", "tcp", "-ac"])
        for _ in range(50):
            if subprocess.run(["xdpyinfo"], env=self.env, capture_output=True).returncode == 0:
                break
            time.sleep(0.1)
        # D-Bus session bus
        out = subprocess.run(["dbus-launch", "--sh-syntax"], env=self.env, capture_output=True, text=True).stdout
        m = re.search(r"DBUS_SESSION_BUS_ADDRESS='([^']+)'", out)
        if m:
            self.env["DBUS_SESSION_BUS_ADDRESS"] = m.group(1)
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = m.group(1)
        os.environ["DISPLAY"] = DISPLAY
        launcher = next((p for p in ("/usr/libexec/at-spi-bus-launcher",
                                      "/usr/lib/at-spi2-core/at-spi-bus-launcher") if Path(p).exists()), None)
        if launcher:
            self._spawn("atspi", [launcher, "--launch-immediately"])
            time.sleep(0.8)
        self._spawn("openbox", ["openbox"])
        time.sleep(0.3)

    def start_wechat(self):
        if self.alive("wechat"):
            return
        self.wechat_started_at = time.time()
        self._spawn("wechat", [WECHAT_BIN])

    def stop_wechat(self, hard: bool = False):
        p = self.procs.get("wechat")
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, 9 if hard else 15)
            except Exception:
                p.kill()
            try:
                p.wait(10)
            except Exception:
                pass
        # stray helper processes
        subprocess.run(["pkill", "-u", str(os.getuid()), "-x", "wechat"], capture_output=True)
        subprocess.run(["pkill", "-u", str(os.getuid()), "-f", "WeChatAppEx"], capture_output=True)

    def restart_wechat(self):
        self.paused = True
        try:
            self.stop_wechat()
            time.sleep(2)
            self.start_wechat()
            self.wechat_restarts += 1
        finally:
            self.paused = False

    def start(self):
        self.start_display()
        self.start_wechat()
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()

    def _watchdog(self):
        while not self._stop.is_set():
            time.sleep(5)
            if not self.alive("xvfb"):
                log.error("Xvfb died; restarting display stack")
                self.start_display()
            if not self.alive("openbox"):
                self._spawn("openbox", ["openbox"])
            if not self.paused and not self.alive("wechat"):
                log.warning("WeChat exited; restarting")
                time.sleep(2)
                self.start_wechat()
                self.wechat_restarts += 1

    def status(self) -> dict:
        return {k: (p.pid if p.poll() is None else f"exited({p.returncode})") for k, p in self.procs.items()} | {
            "wechat_restarts": self.wechat_restarts,
            "wechat_uptime_s": int(time.time() - self.wechat_started_at) if self.alive("wechat") else 0,
        }


def data_dirs() -> dict:
    home = Path(os.environ.get("HOME", "/data/home"))
    cand = [home / "Documents" / "xwechat_files", home / "xwechat_files",
            home / ".local/share/com.tencent.wechat/xwechat_files"]
    return {"home": str(home), "xwechat": str(home / ".xwechat"),
            "files": next((str(c) for c in cand if c.exists()), str(cand[0]))}


def disk_usage() -> dict:
    home = os.environ.get("HOME", "/data")
    try:
        u = shutil.disk_usage(home)
        return {"total_gb": round(u.total / 2**30, 1), "free_gb": round(u.free / 2**30, 1)}
    except Exception:
        return {}
