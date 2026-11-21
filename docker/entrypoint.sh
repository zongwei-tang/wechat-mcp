#!/bin/bash
# Railway mounts volumes owned by root; hand /data to the unprivileged wx user, then drop privileges.
set -e
DATA="${WECHAT_MCP_HOME:-/data}"
mkdir -p "$DATA/home" "$DATA/logs" "$DATA/inbox" "$DATA/outbox"
if [ "$(id -u)" = "0" ]; then
  # only walk the tree when ownership is actually wrong (the volume can hold GBs of WeChat files)
  if [ "$(stat -c %u "$DATA")" != "1000" ] || [ "$(stat -c %u "$DATA/home")" != "1000" ]; then
    chown -R wx:wx "$DATA" 2>/dev/null || true
  fi
  chown wx:wx "$DATA" "$DATA/home" "$DATA/logs" "$DATA/inbox" "$DATA/outbox"
  chown -R wx:wx "$DATA/logs"
  # WeChat records absolute paths (e.g. /home/wx/xwechat_files) in its config, so keep HOME=/home/wx
  # and make it point at the persistent volume.
  if [ ! -L /home/wx ]; then rm -rf /home/wx; ln -s "$DATA/home" /home/wx; fi
  exec runuser -u wx -- env HOME=/home/wx USER=wx python3 -m wechat_mcp "$@"
fi
export HOME="$DATA/home"
exec python3 -m wechat_mcp "$@"
