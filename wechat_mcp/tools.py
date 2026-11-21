"""MCP tool definitions. Every tool runs its WeChat work on the single UI thread."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Literal, Optional

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import runtime as rt
from .state import STATE
from .ui import ui
from .wechat import WX, WeChatError

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

Chat = Annotated[str, Field(description="Chat display name exactly as shown in WeChat (remark, nickname or group name). "
                                        "'文件传输助手' is your own File Transfer chat.")]
MsgText = Annotated[Optional[str], Field(description="Text (or a unique substring) of the target message")]
MsgIndex = Annotated[Optional[int], Field(description="Alternative to message_text: negative position, -1 = newest message")]


def _j(d) -> str:
    return json.dumps(d, ensure_ascii=False, indent=1, default=str)


async def run(fn, *a, timeout: float = 180, **kw):
    try:
        return await ui(fn, *a, timeout=timeout, **kw)
    except WeChatError as e:
        return {"error": str(e)}


def register(mcp: MCPServer):
    def tool(annotations=WRITE, **kw):
        return mcp.tool(annotations=annotations, structured_output=False, **kw)

    # ------------------------------------------------------------------ account
    @tool(RO)
    async def wechat_status() -> str:
        """Login state of the WeChat client, the signed-in account, client process health and recent
        automatic actions (e.g. auto-requested phone confirmations). Call this first."""
        st = await run(WX.login_state)
        out = {"login": st, "login_page": STATE.login_url}
        if STATE.runtime:
            out["processes"] = STATE.runtime.status()
        out["disk"] = rt.disk_usage()
        out["recent_events"] = WX.events[-10:]
        if isinstance(st, dict) and st.get("logged_in"):
            cur = await run(WX.current_chat)
            out["current_chat"] = cur
        return _j(out)

    @tool()
    async def wechat_login(refresh_qr: bool = False) -> list:
        """Start / continue login. Returns the login screen image: a QR code to scan with phone WeChat
        (扫一扫), or the phone-confirmation screen. If the client shows '进入微信/登录' it is clicked so the
        phone gets a confirmation prompt. Also returns a login page URL that auto-refreshes the QR code
        (open it on another screen than the phone)."""
        st = await run(WX.login_state)
        if isinstance(st, dict) and st.get("logged_in"):
            return [_j({"state": "logged_in", "message": "already logged in"})]
        if isinstance(st, dict) and st.get("state") == "confirm_needed":
            st = await run(WX.confirm_login)
        elif refresh_qr:
            st = await run(WX.refresh_qr)
        img = await ui(WX.login_screenshot, False)
        return [_j({"state": st, "login_page": STATE.login_url,
                    "how": "Scan the QR with phone WeChat, then confirm on the phone. Poll wechat_status."}),
                Image(data=img, format="png")]

    @tool(DESTRUCTIVE)
    async def wechat_logout(confirm: bool = False) -> str:
        """Log the cloud WeChat client out (you will need to scan a QR code again). Requires confirm=true."""
        if not confirm:
            return _j({"error": "pass confirm=true to really log out"})
        return _j(await run(WX.logout))

    @tool()
    async def wechat_restart_client() -> str:
        """Restart the WeChat client process (login is kept on disk; the phone may have to confirm again)."""
        if not STATE.runtime:
            return _j({"error": "runtime not managed by this server"})
        await ui(STATE.runtime.restart_wechat, timeout=60)
        await asyncio.sleep(8)
        return _j(await run(WX.login_state))

    @tool(RO)
    async def wechat_my_profile() -> str:
        """Your own account: display name, WeChat ID, region."""
        return _j(await run(WX.my_profile))

    @tool(RO)
    async def wechat_get_settings() -> str:
        """Read the client's checkbox settings (notifications, voice-to-text, keep history, ...)."""
        return _j(await run(WX.get_settings))

    @tool()
    async def wechat_set_setting(name: Annotated[str, Field(description="Exact checkbox label, e.g. '聊天中的语音消息自动转成文字', '新消息通知声音'")],
                                 value: bool) -> str:
        """Turn one client setting (checkbox) on or off."""
        return _j(await run(WX.set_setting, name, value))

    # -------------------------------------------------------------------- chats
    @tool(RO)
    async def wechat_list_chats(limit: Annotated[int, Field(ge=1, le=500)] = 40, folded: bool = False) -> str:
        """List recent chats (the left chat list) with last message preview, time, unread count and muted flag.
        unread=-1 means a red badge is shown but the count is not readable. folded=true lists the chats inside
        '折叠的聊天' (folded/muted groups)."""
        return _j(await run(WX.list_chats, limit, folded))

    @tool(RO)
    async def wechat_unread_chats(limit: Annotated[int, Field(ge=1, le=500)] = 80, include_folded: bool = True) -> str:
        """Only chats that currently have unread messages (including folded chats if include_folded)."""
        chats = await run(WX.list_chats, limit, False)
        if isinstance(chats, dict):
            return _j(chats)
        out = [c for c in chats if c.get("unread") and not c.get("is_folder")]
        if include_folded and any(c.get("is_folder") and (c.get("unread") or c.get("last_message")) for c in chats):
            folded = await run(WX.list_chats, limit, True)
            if isinstance(folded, list):
                out += [dict(c, folded=True) for c in folded if c.get("unread")]
        return _j({"count": len(out), "chats": out})

    @tool(RO)
    async def wechat_current_chat() -> str:
        """Which chat is open in the client right now."""
        return _j(await run(WX.current_chat))

    @tool()
    async def wechat_open_chat(chat: Chat) -> str:
        """Open (switch to) a chat by name; searches contacts/groups if it is not in the recent list.
        Opening a chat marks it as read."""
        return _j(await run(WX.open_chat, chat))

    @tool(RO)
    async def wechat_search(query: str, open_result_index: Annotated[Optional[int], Field(description="Open the N-th result (0-based) after searching")] = None) -> str:
        """Global search (like the client's search box): contacts, group chats, chat history, features."""
        return _j(await run(WX.search, query, False, open_result_index))

    @tool(RO)
    async def wechat_find_chat(query: str) -> str:
        """Find chats/contacts/groups whose name matches `query` (search results only, nothing is opened)."""
        r = await run(WX.search, query, False, None)
        if isinstance(r, dict) and "results" in r:
            r["results"] = [x for x in r["results"] if x.get("section") not in ("搜一搜", "搜索网络结果")]
        return _j(r)

    # ----------------------------------------------------------------- messages
    @tool(RO)
    async def wechat_read_messages(chat: Chat, limit: Annotated[int, Field(ge=1, le=500)] = 30,
                                   resolve_senders: Annotated[bool, Field(description="In groups, click unknown avatars once to learn exact sender names (cached)")] = True,
                                   max_sender_lookups: Annotated[int, Field(ge=0, le=60)] = 12) -> str:
        """Read the latest messages of a chat (scrolls back for older ones). Each message has direction
        (in/out), sender, type (text/image/voice/file/link/...), text and the time separator above it.
        Opening the chat marks it read. Only messages the cloud client has received since login (or imported
        from the phone) are available."""
        return _j(await run(WX.read_messages, chat, limit, resolve_senders, max_sender_lookups, timeout=600))

    @tool(RO)
    async def wechat_read_current_chat(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> str:
        """Read the latest messages of whichever chat is open now (no switching)."""
        return _j(await run(WX.read_messages, None, limit, True, 8, timeout=300))

    @tool(RO)
    async def wechat_read_unread(max_chats: Annotated[int, Field(ge=1, le=50)] = 10,
                                 per_chat_limit: Annotated[int, Field(ge=1, le=100)] = 20,
                                 include_folded: bool = False) -> str:
        """Open every chat with unread messages and return its newest messages (marks them read).
        The number returned per chat is the unread count when known, else per_chat_limit."""
        chats = await run(WX.list_chats, 80, False)
        if isinstance(chats, dict):
            return _j(chats)
        todo = [c for c in chats if c.get("unread") and not c.get("is_folder")]
        if include_folded:
            folded = await run(WX.list_chats, 80, True)
            if isinstance(folded, list):
                todo += [c for c in folded if c.get("unread")]
        out = []
        for c in todo[:max_chats]:
            n = c["unread"] if c["unread"] and c["unread"] > 0 else per_chat_limit
            r = await run(WX.read_messages, c["name"], min(n, per_chat_limit), True, 6, timeout=300)
            out.append({"chat": c["name"], "unread": c["unread"], "messages": r.get("messages") if isinstance(r, dict) else r,
                        **({"error": r["error"]} if isinstance(r, dict) and "error" in r else {})})
        return _j({"chats_read": len(out), "remaining_unread_chats": max(0, len(todo) - max_chats), "results": out})

    @tool(RO)
    async def wechat_wait_for_message(timeout_seconds: Annotated[int, Field(ge=5, le=600)] = 120,
                                      chat: Annotated[Optional[str], Field(description="Only wake for this chat")] = None,
                                      poll_seconds: Annotated[int, Field(ge=2, le=60)] = 5) -> str:
        """Block until a new message arrives (the chat list changes), then return the changed chats."""
        import asyncio
        base = await run(WX.list_chats, 30, False, False)
        if isinstance(base, dict):
            return _j(base)
        key = {c["name"]: (c["last_message"], c["time"], c["unread"]) for c in base}
        end = time.time() + timeout_seconds
        while time.time() < end:
            await asyncio.sleep(poll_seconds)
            now = await run(WX.list_chats, 30, False, False)
            if isinstance(now, dict):
                continue
            changed = [c for c in now if key.get(c["name"]) != (c["last_message"], c["time"], c["unread"])
                       and (chat is None or c["name"] == chat)]
            if changed:
                return _j({"new_activity": True, "chats": changed})
        return _j({"new_activity": False, "waited_seconds": timeout_seconds})

    @tool(RO)
    async def wechat_view_message(chat: Chat, message_text: MsgText = None, index: MsgIndex = -1) -> list:
        """Screenshot of one message bubble (use for images, cards, stickers, files, links)."""
        try:
            img = await ui(WX.view_message, chat, message_text, None if message_text else index)
        except WeChatError as e:
            return [_j({"error": str(e)})]
        return [Image(data=img, format="png")]

    @tool(RO)
    async def wechat_open_image(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> list:
        """Open an image message in WeChat's viewer and return a full-screen screenshot of it."""
        try:
            img = await ui(WX.open_image, chat, message_text, index)
        except WeChatError as e:
            return [_j({"error": str(e)})]
        return [Image(data=img, format="png")]

    @tool(RO)
    async def wechat_chat_screenshot(chat: Annotated[Optional[str], Field(description="Chat to open first; omit for the current one")] = None) -> list:
        """Screenshot of the visible conversation (useful to see stickers, images and layout as a human would)."""
        try:
            img = await ui(WX.chat_screenshot, chat)
        except WeChatError as e:
            return [_j({"error": str(e)})]
        return [Image(data=img, format="png")]

    @tool(RO)
    async def wechat_search_chat_history(chat: Chat, keyword: str) -> str:
        """Search one chat's history with the client's 聊天记录 search."""
        return _j(await run(WX.search_chat_history, chat, keyword))

    @tool(RO)
    async def wechat_copy_message(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> str:
        """Copy a message via its 复制 menu item and return the clipboard text (full text of long messages)."""
        return _j(await run(WX.copy_message, chat, message_text, index))

    # --------------------------------------------------------------------- send
    @tool()
    async def wechat_send_message(chat: Chat, text: str,
                                  mention: Annotated[Optional[list[str]], Field(description="Group members to @ (display names)")] = None,
                                  mention_all: Annotated[bool, Field(description="@所有人 (group owner/admin only)")] = False) -> str:
        """Send a text message as the user (emoji and newlines are fine). Verifies it appears in the chat.
        Sends as the real account — confirm with the user before messaging other people."""
        return _j(await run(WX.send_text, chat, text, mention, mention_all))

    @tool()
    async def wechat_send_messages(chat: Chat, messages: list[str],
                                   delay_seconds: Annotated[float, Field(ge=0.5, le=30)] = 1.5) -> str:
        """Send several text messages in order to one chat."""
        import asyncio
        res = []
        for m in messages:
            res.append(await run(WX.send_text, chat, m, None, False))
            await asyncio.sleep(delay_seconds)
        return _j(res)

    @tool()
    async def wechat_broadcast(chats: list[str], text: str,
                               delay_seconds: Annotated[float, Field(ge=1, le=60)] = 3) -> str:
        """Send the same text to several chats one by one (keep lists short; WeChat rate-limits spam)."""
        import asyncio
        res = []
        for c in chats:
            res.append(await run(WX.send_text, c, text, None, False))
            await asyncio.sleep(delay_seconds)
        return _j(res)

    @tool()
    async def wechat_reply(chat: Chat, text: str, quote_text: MsgText = None, quote_index: MsgIndex = None) -> str:
        """Quote-reply (引用) a specific message: pick it by text or by index (-1 = newest)."""
        return _j(await run(WX.reply, chat, text, quote_text, quote_index))

    @tool()
    async def wechat_send_image(chat: Chat,
                                path: Annotated[Optional[str], Field(description="Path on the server")] = None,
                                url: Annotated[Optional[str], Field(description="http(s) URL to download")] = None,
                                data_base64: Optional[str] = None,
                                filename: Optional[str] = None,
                                caption: Optional[str] = None) -> str:
        """Send an image (from a server path, a URL, or base64 data)."""
        try:
            p = await ui(WX._materialize, path, url, data_base64, filename or "image.png")
        except Exception as e:
            return _j({"error": str(e)})
        return _j(await run(WX.send_files, chat, [p], caption))

    @tool()
    async def wechat_send_file(chat: Chat,
                               path: Annotated[Optional[str], Field(description="Path on the server")] = None,
                               url: Annotated[Optional[str], Field(description="http(s) URL to download")] = None,
                               data_base64: Optional[str] = None,
                               filename: Optional[str] = None,
                               caption: Optional[str] = None) -> str:
        """Send any file (document, video, archive...) from a server path, URL or base64 data."""
        try:
            p = await ui(WX._materialize, path, url, data_base64, filename)
        except Exception as e:
            return _j({"error": str(e)})
        return _j(await run(WX.send_files, chat, [p], caption))

    @tool()
    async def wechat_send_files(chat: Chat, paths: list[str]) -> str:
        """Send several files that are already on the server, in one go."""
        return _j(await run(WX.send_files, chat, paths, None))

    @tool()
    async def wechat_forward_message(chat: Chat, to: list[str], message_text: MsgText = None,
                                     index: MsgIndex = None, comment: Optional[str] = None) -> str:
        """Forward (转发) a message from `chat` to one or more other chats."""
        return _j(await run(WX.forward, chat, to, message_text, index, comment))

    @tool(DESTRUCTIVE)
    async def wechat_recall_message(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> str:
        """Recall (撤回) one of your own messages (only possible within ~2 minutes of sending)."""
        return _j(await run(WX.recall, chat, message_text, index))

    @tool(DESTRUCTIVE)
    async def wechat_delete_message(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> str:
        """Delete a message from this device's chat view (does not recall it for others)."""
        return _j(await run(WX.delete_message, chat, message_text, index))

    @tool(RO)
    async def wechat_message_options(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> str:
        """List the right-click menu actions available for a message (复制, 转发, 收藏, 引用, 撤回, 翻译, ...)."""
        return _j(await run(WX.message_menu_options, chat, message_text, index))

    @tool()
    async def wechat_message_action(chat: Chat, action: Annotated[str, Field(description="Menu label, e.g. 收藏, 翻译, 多选, 提醒, 放大阅读, 搜一搜, 转文字")],
                                    message_text: MsgText = None, index: MsgIndex = None) -> str:
        """Run any right-click menu action on a message (e.g. 收藏 to favorite, 翻译 to translate, 转文字 for voice)."""
        return _j(await run(WX.message_action, chat, action, message_text, index))

    @tool()
    async def wechat_favorite_message(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> str:
        """Add a message to 收藏 (favorites)."""
        return _j(await run(WX.message_action, chat, "收藏", message_text, index))

    @tool()
    async def wechat_translate_message(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> str:
        """Translate a message with WeChat's built-in translation, then read the chat to see the result."""
        r = await run(WX.message_action, chat, "翻译", message_text, index)
        await asyncio.sleep(2)
        msgs = await run(WX.read_messages, chat, 6, False, 0)
        return _j({"action": r, "latest": msgs.get("messages") if isinstance(msgs, dict) else msgs})

    @tool()
    async def wechat_voice_to_text(chat: Chat, message_text: MsgText = None, index: MsgIndex = None) -> str:
        """Convert a voice message to text (转文字) and return the chat's latest messages."""
        r = await run(WX.message_action, chat, "转文字", message_text, index)
        await asyncio.sleep(3)
        msgs = await run(WX.read_messages, chat, 6, False, 0)
        return _j({"action": r, "latest": msgs.get("messages") if isinstance(msgs, dict) else msgs})

    # ------------------------------------------------------------ chat actions
    @tool(RO)
    async def wechat_chat_options(chat: Chat) -> str:
        """List the right-click menu actions for a chat in the chat list (置顶, 标为未读, 消息免打扰, ...)."""
        return _j(await run(WX.session_options, chat))

    @tool()
    async def wechat_chat_action(chat: Chat, action: Annotated[str, Field(description="Exact menu label, e.g. 置顶 / 取消置顶 / 标为未读 / 标为已读 / 消息免打扰 / 允许消息通知 / 独立窗口显示 / 不显示 / 移出「折叠的聊天」")]) -> str:
        """Run a chat-list context menu action on a chat."""
        if action in ("删除",):
            return _j({"error": "use wechat_delete_chat for deletion"})
        return _j(await run(WX.session_action, chat, action))

    @tool()
    async def wechat_pin_chat(chat: Chat, pinned: bool = True) -> str:
        """Pin (置顶) or unpin a chat."""
        return _j(await run(WX.session_action, chat, "置顶" if pinned else "取消置顶"))

    @tool()
    async def wechat_mute_chat(chat: Chat, muted: bool = True) -> str:
        """Mute (消息免打扰) or unmute (允许消息通知) a chat."""
        return _j(await run(WX.session_action, chat, "消息免打扰" if muted else "允许消息通知"))

    @tool()
    async def wechat_mark_unread(chat: Chat) -> str:
        """Mark a chat as unread (标为未读)."""
        return _j(await run(WX.session_action, chat, "标为未读"))

    @tool()
    async def wechat_mark_read(chat: Chat) -> str:
        """Mark a chat as read (opens it, or uses 标为已读 when offered)."""
        r = await run(WX.session_options, chat)
        if isinstance(r, dict) and "标为已读" in r.get("options", []):
            return _j(await run(WX.session_action, chat, "标为已读"))
        return _j(await run(WX.open_chat, chat))

    @tool()
    async def wechat_hide_chat(chat: Chat) -> str:
        """Hide a chat from the list (不显示); it comes back when a new message arrives."""
        return _j(await run(WX.session_action, chat, "不显示"))

    @tool(DESTRUCTIVE)
    async def wechat_delete_chat(chat: Chat, confirm: bool = False) -> str:
        """Delete a chat and its local history from this device (删除). Requires confirm=true."""
        if not confirm:
            return _j({"error": "pass confirm=true to delete the chat from this device"})
        return _j(await run(WX.session_action, chat, "删除"))

    @tool()
    async def wechat_import_history_from_phone(chat: Annotated[Optional[str], Field(description="Open this chat first")] = None) -> str:
        """Start WeChat's official 'import chat history from phone' flow so older messages become readable here."""
        return _j(await run(WX.import_history_from_phone, chat))

    # -------------------------------------------------------- contacts / groups
    @tool(RO)
    async def wechat_list_contacts(limit: Annotated[int, Field(ge=1, le=5000)] = 2000) -> str:
        """All contacts from 通讯录 (scrolls the whole list), plus section counts (公众号, 企业微信联系人, ...)."""
        return _j(await run(WX.list_contacts, limit, timeout=900))

    @tool(RO)
    async def wechat_contacts_with_remarks(query: Annotated[Optional[str], Field(description="Optional filter text")] = None,
                                           limit: Annotated[int, Field(ge=1, le=5000)] = 3000) -> str:
        """Contacts from the 通讯录管理 window with nickname, remark (备注) and tags (标签) per row."""
        return _j(await run(WX.contacts_manager, limit, query, timeout=900))

    @tool(RO)
    async def wechat_contact_info(name: Annotated[str, Field(description="Contact display name")]) -> str:
        """Profile card of a contact: remark, nickname, WeChat ID, region, tags, common groups, source, added date."""
        return _j(await run(WX.contact_info, name))

    @tool(RO)
    async def wechat_group_members(group: Annotated[str, Field(description="Group display name")],
                                   limit: Annotated[int, Field(ge=1, le=2000)] = 500) -> str:
        """Members of a group chat (display names) and the member count."""
        return _j(await run(WX.group_members, group, limit, timeout=600))

    @tool(RO)
    async def wechat_group_member_info(group: str, member: str) -> str:
        """Profile card of one group member (works for non-friends too)."""
        return _j(await run(WX.group_member_info, group, member))

    @tool(RO)
    async def wechat_group_info(group: str) -> str:
        """Group name, member count and members (first page)."""
        r = await run(WX.group_members, group, 60)
        return _j(r)

    @tool(RO)
    async def wechat_friend_requests() -> str:
        """Pending friend requests (通讯录 → 新的朋友)."""
        return _j(await run(WX.new_friend_requests))

    @tool()
    async def wechat_accept_friend_request(name: str, remark: Optional[str] = None) -> str:
        """Accept a pending friend request from `name` (optionally set a remark)."""
        return _j(await run(WX.accept_friend_request, name, remark))

    @tool()
    async def wechat_add_friend(query: Annotated[str, Field(description="WeChat ID or phone number")],
                                greeting: Optional[str] = None, remark: Optional[str] = None) -> str:
        """Search a WeChat ID / phone number and send a friend request."""
        return _j(await run(WX.add_friend, query, greeting, remark))

    @tool()
    async def wechat_create_group(members: Annotated[list[str], Field(description="At least 2 contact display names")]) -> str:
        """Create a new group chat with the given contacts (发起群聊)."""
        return _j(await run(WX.create_group, members))

    # ---------------------------------------------------- favorites / moments
    @tool(RO)
    async def wechat_list_favorites(limit: Annotated[int, Field(ge=1, le=200)] = 50) -> str:
        """Items in 收藏 (favorites)."""
        return _j(await run(WX.list_favorites, limit))

    @tool(RO)
    async def wechat_moments(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> str:
        """Read the Moments (朋友圈) feed as text."""
        return _j(await run(WX.moments, limit, timeout=300))

    # --------------------------------------------------------------- low level
    @tool(RO)
    async def wechat_screenshot(scale: Annotated[float, Field(ge=0.25, le=1.0)] = 0.75) -> list:
        """Screenshot of the whole virtual screen of the cloud WeChat client."""
        from . import xinput
        img = await ui(xinput.screenshot, None, None, scale)
        return [Image(data=img, format="png")]

    @tool(RO)
    async def wechat_ui_tree(only_visible: bool = True, popups_only: bool = False,
                             max_nodes: Annotated[int, Field(ge=50, le=5000)] = 1200) -> str:
        """Accessibility tree of the WeChat window(s) — role, name and position of every element.
        Use with wechat_click_element for anything not covered by a dedicated tool."""
        r = await run(WX.ui_tree, only_visible, max_nodes, popups_only)
        return r if isinstance(r, str) else _j(r)

    @tool()
    async def wechat_click_element(name: str, role: Optional[str] = None, index: int = 0,
                                   button: Literal["left", "right"] = "left", double: bool = False) -> str:
        """Click a UI element by its accessible name (exact, else substring), e.g. a button or menu item."""
        return _j(await run(WX.click_element, name, role, index, 1 if button == "left" else 3, double))

    @tool()
    async def wechat_click(x: int, y: int, button: Literal["left", "right"] = "left", double: bool = False) -> str:
        """Click at screen pixel coordinates (as seen in wechat_screenshot at scale 1.0)."""
        from . import xinput
        def go():
            if double:
                xinput.double_click(x, y)
            else:
                xinput.click(x, y, 1 if button == "left" else 3)
        await ui(go)
        return _j({"clicked": [x, y]})

    @tool()
    async def wechat_press_key(keys: Annotated[str, Field(description="xdotool key names, space separated, e.g. 'Escape', 'ctrl+f', 'Return'")]) -> str:
        """Press keyboard keys in the WeChat client."""
        from . import xinput
        await ui(xinput.key, *keys.split())
        return _j({"pressed": keys})

    @tool()
    async def wechat_type_text(text: str, press_enter: bool = False) -> str:
        """Paste text at the current focus (any language), optionally followed by Enter."""
        from . import xinput
        def go():
            xinput.set_clipboard_text(text)
            xinput.key("ctrl+v")
            if press_enter:
                xinput.key("Return")
        await ui(go)
        return _j({"typed": len(text)})

    @tool()
    async def wechat_scroll(x: int, y: int, clicks: Annotated[int, Field(description="positive = up, negative = down")]) -> str:
        """Scroll with the mouse wheel at screen coordinates."""
        from . import xinput
        await ui(xinput.scroll, x, y, clicks)
        return _j({"scrolled": clicks})
