# wechat-mcp

[English](README.md) | **简体中文**

把**你自己的个人微信**接成一个 MCP 服务器（Streamable HTTP），可以部署在 Railway 或任何 Linux 云容器上，不需要 Mac。

## 原理

容器里运行腾讯官方的 **微信 Linux 版**（每次构建都从 `linux.weixin.qq.com` 的 CDN 拉最新版），跑在虚拟显示器 Xvfb 上：

- **读**：通过 AT-SPI 无障碍接口（读屏软件用的那套）读取会话列表、消息、联系人、群成员、资料卡。
- **写**：用 X11 模拟键盘鼠标，加上剪贴板，来发文字、图片、文件，以及引用、转发、右键菜单等操作。
- **群聊发送人**：消息里读不到发送人，所以点一下头像读资料卡，再按头像图案缓存，同一个人只点一次。
- **不碰协议、不注入进程、不解密数据库**：用的就是官方客户端，相当于一个人在远程操作电脑版微信。

### 局限

- Linux 版和 Mac/Windows 版共用“电脑端”登录位，同时登录会互相挤下线。
- 只能读到客户端登录之后收到的消息。更早的记录用 `wechat_import_history_from_phone`（官方的“从手机导入聊天记录”）。
- 群公告、群名修改等只在界面上画出来、无障碍接口读不到的设置项，暂时没做。
- 所有操作走同一个图形界面，按顺序执行，单个操作通常要 2 到 10 秒。

## 登录与保活

- 首次：打开 `/login/<token>` 页面（二维码每 2.5 秒自动刷新），用手机微信扫码，然后在手机上确认；也可以调用 `wechat_login` 工具拿二维码图片。
- 登录状态保存在 `/data` 卷上，重启或重新部署都不需要再扫码。
- 后台守护线程每 30 秒检查一次。客户端掉回“登录 / 进入微信”界面时，会自动点击，让你手机弹出确认；你不在时按 10 分钟、30 分钟、1.5 小时、最长 4 小时的间隔重试，避免刷屏。
- 手机确认页上如果有“自动登录该设备”，打开它可以免去以后的确认。
- 登录后会自动关闭通知声音（无头环境里会导致不稳定），并打开“语音消息自动转文字”。
- `/profile-export/<token>` 和 `/profile-import/<token>` 用来导出、导入登录设备信息，可以把已登录的设备整体搬到另一台服务器。

## 端点

| 路径 | 说明 |
|---|---|
| `/mcp/<token>` | Streamable HTTP MCP 端点 |
| `/login/<token>` | 登录页（实时二维码、手机确认状态） |
| `/profile-export/<token>` · `/profile-import/<token>` | 登录设备信息导出 / 导入（tar.gz） |
| `/health` | 健康检查 |

`<token>` = 环境变量 `MCP_AUTH_TOKEN`（未设置时自动生成并写到 `/data/mcp_token`）。

## 部署（Railway）

1. 新建服务，从本仓库的 Dockerfile 构建（`railway.json` 已配置）。
2. 挂一个 Volume 到 `/data`（必须，否则每次部署都要重新登录）。
3. 设置变量 `MCP_AUTH_TOKEN`（长随机串）。可选：`PUBLIC_URL`、`LOGIN_WATCH_SECONDS`。
4. 生成公网域名，打开 `https://<域名>/login/<token>` 扫码。
5. 在 Claude 里添加自定义连接器：`https://<域名>/mcp/<token>`。

建议资源：2 GB 内存以上。建议把部署触发路径限制在代码目录（`wechat_mcp/**`、`Dockerfile`、`docker/**`、`requirements.txt`），因为每次重新部署都会重启微信、需要手机确认。

本地运行：

```bash
docker build -t wechat-mcp .
docker run -d --shm-size=1g -p 8000:8000 -e MCP_AUTH_TOKEN=change-me -v wechat-data:/data wechat-mcp
```

## 工具一览（65 个）

**账号**：`wechat_status` `wechat_login` `wechat_logout` `wechat_restart_client` `wechat_my_profile` `wechat_get_settings` `wechat_set_setting`

**会话**：`wechat_list_chats` `wechat_unread_chats` `wechat_current_chat` `wechat_open_chat` `wechat_search` `wechat_find_chat` `wechat_chat_options` `wechat_chat_action` `wechat_pin_chat` `wechat_mute_chat` `wechat_mark_unread` `wechat_mark_read` `wechat_hide_chat` `wechat_delete_chat` `wechat_import_history_from_phone`

**读消息**：`wechat_read_messages` `wechat_read_current_chat` `wechat_read_unread` `wechat_wait_for_message` `wechat_view_message` `wechat_open_image` `wechat_chat_screenshot` `wechat_search_chat_history` `wechat_copy_message`

**发消息**：`wechat_send_message`（支持 @ 成员、@所有人）`wechat_send_messages` `wechat_broadcast` `wechat_reply`（引用回复）`wechat_send_image` `wechat_send_file` `wechat_send_files` `wechat_forward_message` `wechat_recall_message` `wechat_delete_message`

**消息菜单**：`wechat_message_options` `wechat_message_action` `wechat_favorite_message` `wechat_translate_message` `wechat_voice_to_text`

**联系人与群**：`wechat_list_contacts` `wechat_contacts_with_remarks` `wechat_contact_info` `wechat_group_members` `wechat_group_member_info` `wechat_group_info` `wechat_friend_requests` `wechat_accept_friend_request` `wechat_add_friend` `wechat_create_group`

**收藏 / 朋友圈**：`wechat_list_favorites` `wechat_moments`

**底层兜底**：`wechat_screenshot` `wechat_ui_tree` `wechat_click_element` `wechat_click` `wechat_press_key` `wechat_type_text` `wechat_scroll`

## 风险提示

这是用你自己的账号做自动化，腾讯可能对异常行为风控。避免高频群发；数据中心 IP 也可能触发额外验证。本项目与腾讯无关，请只用于你自己的账号。

## 许可证

MIT
