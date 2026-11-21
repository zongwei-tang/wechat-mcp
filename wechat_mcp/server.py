"""Entry point: start the GUI runtime and serve MCP over Streamable HTTP.

URL layout (token = MCP_AUTH_TOKEN):
  /mcp/<token>        Streamable HTTP MCP endpoint
  /login/<token>      live login page (QR / phone-confirm screen, auto refresh)
  /screen/<token>.png current screenshot (used by the login page)
  /health             unauthenticated liveness probe
"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import secrets
import sys
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import runtime as rt
from .state import STATE

log = logging.getLogger("wechat_mcp")

INSTRUCTIONS = """Drive the user's own personal WeChat (微信) account through the official WeChat for Linux client running in this server.
Start with wechat_status. If it says the account is not logged in, call wechat_login and show the user the returned QR image (scan with phone WeChat), or give them the login page URL; then poll wechat_status.
Reading: wechat_list_chats / wechat_unread_chats → wechat_read_messages(chat). Search with wechat_search (contacts, groups, chat history). Contacts: wechat_list_contacts, wechat_contact_info. Groups: wechat_group_members, wechat_group_info.
Sending: wechat_send_message(chat, text, mention=[...]), wechat_reply(chat, quote_text, text), wechat_send_image / wechat_send_file. Sends act as the user — confirm with the user before messaging real people unless they asked explicitly.
Chats are addressed by their display name (remark/nickname/group name) exactly as shown in WeChat; use wechat_search or wechat_find_chat when unsure.
Everything goes through one GUI, so calls are serialised; long operations (history scroll-back, bulk send) take seconds to minutes.
If something looks wrong, wechat_screenshot shows the screen and wechat_ui_tree dumps the accessibility tree; wechat_click / wechat_press_key / wechat_click_element are low-level escape hatches."""


def build_server() -> MCPServer:
    mcp = MCPServer(name="wechat-mcp", title="WeChat (personal account)", instructions=INSTRUCTIONS,
                    version="0.1.0")
    from . import tools  # noqa: F401  (registers tools)
    tools.register(mcp)
    return mcp


LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>WeChat MCP 登录</title>
<style>body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;background:#f5f5f5;margin:0;padding:16px;text-align:center;color:#222}
img{max-width:100%;border:1px solid #ddd;border-radius:8px;background:#fff}#s{margin:12px 0;font-size:15px}
button{font-size:15px;padding:8px 14px;margin:4px;border-radius:6px;border:1px solid #ccc;background:#fff}</style></head>
<body><h2>微信 MCP 登录</h2><div id="s">加载中…</div><img id="q" alt="screen">
<div><button onclick="act('refresh_qr')">刷新二维码</button><button onclick="act('confirm')">点击“进入微信/登录”</button>
<button onclick="act('full')">显示整个屏幕</button></div>
<p style="font-size:13px;color:#666">用手机微信“扫一扫”扫码；若显示“需在手机上完成登录”，请在手机上确认。</p>
<script>
const T=location.pathname.split('/').pop();let full=false;
async function tick(){try{const r=await fetch('/login-state/'+T);const j=await r.json();
document.getElementById('s').textContent=j.message||j.state;
document.getElementById('q').src='/screen/'+T+'.png?'+(full?'full=1&':'')+Date.now();}catch(e){}}
async function act(a){if(a==='full'){full=!full;return tick();}await fetch('/login-action/'+T+'?a='+a,{method:'POST'});setTimeout(tick,800);}
tick();setInterval(tick,2500);
</script></body></html>"""


PROFILE_PARTS = (".xwechat", "xwechat_files")


def _profile_tar() -> bytes:
    import io
    import tarfile
    home = Path(os.environ.get("HOME", "/home/wx"))
    rt_ = STATE.runtime
    if rt_:
        rt_.paused = True
        rt_.stop_wechat()
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for part in PROFILE_PARTS:
                p = home / part
                if p.exists():
                    tf.add(str(p), arcname=part)
        return buf.getvalue()
    finally:
        if rt_:
            rt_.start_wechat()
            rt_.paused = False


def _profile_restore(data: bytes) -> int:
    import io
    import shutil
    import tarfile
    home = Path(os.environ.get("HOME", "/home/wx")).resolve()
    tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    members = tf.getmembers()
    for m in members:
        name = m.name
        if name.startswith("/") or ".." in Path(name).parts or not name.split("/")[0] in PROFILE_PARTS:
            raise ValueError(f"unsafe or unexpected path in archive: {name}")
        if m.issym() or m.islnk():
            target = (home / Path(name).parent / m.linkname).resolve()
            if not str(target).startswith(str(home)):
                raise ValueError(f"link escapes profile dir: {name}")
        if m.isdev():
            raise ValueError(f"device file in archive: {name}")
    rt_ = STATE.runtime
    if rt_:
        rt_.paused = True
        rt_.stop_wechat()
    try:
        for part in PROFILE_PARTS:
            shutil.rmtree(home / part, ignore_errors=True)
        tf.extractall(str(home), members=members)
        return len(members)
    finally:
        if rt_:
            rt_.start_wechat()
            rt_.paused = False


def start_login_watcher():
    """Background keep-alive: watch the login state, auto-request phone confirmation when the
    client drops back to the '进入微信' screen, dismiss modal noise, apply recommended settings."""
    import threading
    import time as _t

    from .ui import ui_sync
    from .wechat import WX

    interval = float(os.environ.get("LOGIN_WATCH_SECONDS", "30"))

    def loop():
        _t.sleep(15)
        while True:
            try:
                ui_sync(WX.watch_tick)
            except Exception as e:  # pragma: no cover
                log.debug("watch tick failed: %s", e)
            _t.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="login-watcher").start()


def main():
    ap = argparse.ArgumentParser(prog="wechat_mcp")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve"])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--no-gui", action="store_true", help="do not start Xvfb/WeChat (attach to an existing DISPLAY)")
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    home = Path(os.environ.get("WECHAT_MCP_HOME", "/data"))
    home.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not token:
        tf = home / "mcp_token"
        if tf.exists():
            token = tf.read_text().strip()
        else:
            token = secrets.token_urlsafe(24)
            tf.write_text(token)
            tf.chmod(0o600)
        log.info("MCP_AUTH_TOKEN not set; using token stored in %s", tf)

    if not args.no_gui:
        STATE.runtime = rt.Runtime()
        STATE.runtime.start()
    start_login_watcher()

    mcp = build_server()

    def ok(t: str) -> bool:
        return hmac.compare_digest(t.encode(), token.encode())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    @mcp.custom_route("/login/{t}", methods=["GET"])
    async def login_page(request: Request) -> Response:
        if not ok(request.path_params["t"]):
            return Response(status_code=404)
        return HTMLResponse(LOGIN_HTML)

    @mcp.custom_route("/login-state/{t}", methods=["GET"])
    async def login_state(request: Request) -> Response:
        if not ok(request.path_params["t"]):
            return Response(status_code=404)
        from .wechat import WX
        from .ui import ui
        st = await ui(WX.login_state)
        return JSONResponse(st)

    @mcp.custom_route("/login-action/{t}", methods=["POST"])
    async def login_action(request: Request) -> Response:
        if not ok(request.path_params["t"]):
            return Response(status_code=404)
        from .wechat import WX
        from .ui import ui
        a = request.query_params.get("a", "")
        if a == "refresh_qr":
            r = await ui(WX.refresh_qr)
        elif a == "confirm":
            r = await ui(WX.confirm_login)
        else:
            r = {"error": "unknown action"}
        return JSONResponse(r)

    @mcp.custom_route("/screen/{t}", methods=["GET"])
    async def screen(request: Request) -> Response:
        t = request.path_params["t"]
        if t.endswith(".png"):
            t = t[:-4]
        if not ok(t):
            return Response(status_code=404)
        from .wechat import WX
        from .ui import ui
        full = request.query_params.get("full") == "1"
        data = await ui(WX.login_screenshot, full)
        return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/profile-export/{t}", methods=["GET"])
    async def profile_export(request: Request) -> Response:
        """Download the WeChat login profile (tar.gz) — lets you move a logged-in device elsewhere."""
        if not ok(request.path_params["t"]):
            return Response(status_code=404)
        from .ui import ui
        data = await ui(_profile_tar, timeout=600)
        return Response(data, media_type="application/gzip",
                        headers={"Content-Disposition": "attachment; filename=wechat-profile.tar.gz"})

    @mcp.custom_route("/profile-import/{t}", methods=["POST"])
    async def profile_import(request: Request) -> Response:
        """Replace the WeChat profile with an uploaded tar.gz (from /profile-export) and restart WeChat."""
        if not ok(request.path_params["t"]):
            return Response(status_code=404)
        body = await request.body()
        from .ui import ui
        try:
            n = await ui(_profile_restore, body, timeout=600)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"restored_files": n, "note": "WeChat restarted; check wechat_status"})

    STATE.public_base = os.environ.get("PUBLIC_URL", "").rstrip("/") or (
        f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else
        f"http://localhost:{args.port}")
    STATE.login_url = f"{STATE.public_base}/login/{token}"

    app = mcp.streamable_http_app(
        streamable_http_path=f"/mcp/{token}",
        stateless_http=True,
        json_response=True,
        host=args.host,
        max_request_body_size=64 * 1024 * 1024,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    log.info("MCP endpoint: %s/mcp/<token>   login page: %s/login/<token>", STATE.public_base, STATE.public_base)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", proxy_headers=True,
                forwarded_allow_ips="*")
