# wechat-mcp

**English** | [简体中文](README.zh-CN.md)

An MCP server (Streamable HTTP) for **your own personal WeChat account**. It runs in a Linux container (Railway or any cloud host) — no Mac or Windows machine needed.

> Not affiliated with Tencent. Use it only with your own account.

## How it works

The container runs the **official WeChat for Linux** client (the latest build is downloaded from Tencent's CDN at image build time) on a virtual X display (Xvfb):

- **Reading** — the AT-SPI accessibility tree (the interface screen readers use) exposes the chat list, messages, contacts, group members and profile cards.
- **Acting** — simulated X11 keyboard/mouse input plus the clipboard send text, images and files, and drive quote-replies, forwarding and context menus.
- **Group senders** — message rows do not expose the sender, so the server clicks the avatar once, reads the profile card, and caches the name by avatar image.
- **No protocol emulation, no process injection, no database decryption** — it is the genuine client, operated the way a person would operate WeChat on a remote desktop.

### Limitations

- The Linux client shares the "desktop" login slot with WeChat for Mac/Windows; logging in on one signs the other out.
- Only messages the client has received since it logged in are readable. For older history use `wechat_import_history_from_phone` (WeChat's official "import chat history from phone").
- Settings that are drawn on screen but not exposed to accessibility (group announcement, renaming a group, …) are not implemented.
- Everything goes through one GUI, so calls run one at a time; a typical call takes 2–10 seconds.

## Login and keep-alive

- First login: open `/login/<token>` (the QR code refreshes every 2.5 s), scan it with phone WeChat and confirm on the phone — or call the `wechat_login` tool to get the QR image.
- The login profile lives on the `/data` volume, so restarts and redeploys do not need a new QR scan.
- A background watcher checks every 30 s. If the client drops back to the "Log in / Enter WeChat" screen it clicks the button so your phone gets a confirmation prompt, retrying with back-off (10 min, 30 min, 1.5 h, then every 4 h) so an absent phone is not spammed.
- If the phone's confirmation screen offers "auto-login on this device", turning it on avoids future confirmations.
- After login the server turns off notification sounds (they destabilise headless clients) and turns on voice-message-to-text.
- `/profile-export/<token>` and `/profile-import/<token>` move a logged-in device to another server.

## Endpoints

| Path | Purpose |
|---|---|
| `/mcp/<token>` | Streamable HTTP MCP endpoint |
| `/login/<token>` | Login page (live QR code, phone-confirmation status) |
| `/profile-export/<token>` · `/profile-import/<token>` | Export / import the login profile (tar.gz) |
| `/health` | Health check |

`<token>` is the `MCP_AUTH_TOKEN` environment variable (if unset, one is generated and stored in `/data/mcp_token`).

## Deploy on Railway

1. Create a service that builds from this repository's Dockerfile.
2. Attach a volume at `/data` (required — otherwise every deploy needs a new login).
3. Set `MCP_AUTH_TOKEN` to a long random string. Optional: `PUBLIC_URL`, `LOGIN_WATCH_SECONDS`.
4. Generate a public domain and open `https://<domain>/login/<token>` to scan the QR code.
5. Add a custom connector in Claude: `https://<domain>/mcp/<token>`.

Recommended: 2 GB RAM or more. Tip: restrict deploy triggers to code paths (`wechat_mcp/**`, `Dockerfile`, `docker/**`, `requirements.txt`) — every redeploy restarts WeChat and asks your phone to confirm.

Run locally:

```bash
docker build -t wechat-mcp .
docker run -d --shm-size=1g -p 8000:8000 -e MCP_AUTH_TOKEN=change-me -v wechat-data:/data wechat-mcp
```

## Tools (65)

**Account**: `wechat_status` `wechat_login` `wechat_logout` `wechat_restart_client` `wechat_my_profile` `wechat_get_settings` `wechat_set_setting`

**Chats**: `wechat_list_chats` `wechat_unread_chats` `wechat_current_chat` `wechat_open_chat` `wechat_search` `wechat_find_chat` `wechat_chat_options` `wechat_chat_action` `wechat_pin_chat` `wechat_mute_chat` `wechat_mark_unread` `wechat_mark_read` `wechat_hide_chat` `wechat_delete_chat` `wechat_import_history_from_phone`

**Reading**: `wechat_read_messages` `wechat_read_current_chat` `wechat_read_unread` `wechat_wait_for_message` `wechat_view_message` `wechat_open_image` `wechat_chat_screenshot` `wechat_search_chat_history` `wechat_copy_message`

**Sending**: `wechat_send_message` (with @mentions / @all) `wechat_send_messages` `wechat_broadcast` `wechat_reply` (quote-reply) `wechat_send_image` `wechat_send_file` `wechat_send_files` `wechat_forward_message` `wechat_recall_message` `wechat_delete_message`

**Message menu**: `wechat_message_options` `wechat_message_action` `wechat_favorite_message` `wechat_translate_message` `wechat_voice_to_text`

**Contacts & groups**: `wechat_list_contacts` `wechat_contacts_with_remarks` `wechat_contact_info` `wechat_group_members` `wechat_group_member_info` `wechat_group_info` `wechat_friend_requests` `wechat_accept_friend_request` `wechat_add_friend` `wechat_create_group`

**Favorites / Moments**: `wechat_list_favorites` `wechat_moments`

**Low-level fallbacks**: `wechat_screenshot` `wechat_ui_tree` `wechat_click_element` `wechat_click` `wechat_press_key` `wechat_type_text` `wechat_scroll`

## Risks

This automates your own account, and Tencent may apply risk controls to unusual behaviour. Avoid high-volume broadcasting; data-centre IPs can also trigger extra verification.

## License

MIT
