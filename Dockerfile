FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
  xvfb xdotool xclip x11-utils xauth openbox dbus-x11 locales ca-certificates curl procps tini \
  at-spi2-core python3 python3-pip python3-pyatspi gir1.2-atspi-2.0 python3-gi imagemagick \
  fonts-noto-cjk fonts-noto-color-emoji libnss3 libasound2 libpulse0 libgl1 libegl1 libgbm1 libdrm2 \
  libxkbcommon0 libxkbcommon-x11-0 libxcb1 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
  libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxcb-cursor0 libxcb-glx0 \
  libxcb-shm0 libxcb-sync1 libxcb-util1 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxss1 libatk1.0-0 \
  libatk-bridge2.0-0 libgtk-3-0 libcups2 libsecret-1-0 libnotify4 libatomic1 libudev1 libbz2-1.0 \
  && sed -i 's/# zh_CN.UTF-8/zh_CN.UTF-8/' /etc/locale.gen && locale-gen && rm -rf /var/lib/apt/lists/*
# Official WeChat for Linux (always the current release from Tencent's CDN)
ARG WECHAT_DEB_URL=https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb
RUN curl -fsSL -o /tmp/wechat.deb "$WECHAT_DEB_URL" && apt-get update && apt-get install -y /tmp/wechat.deb \
  && rm -f /tmp/wechat.deb && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt
COPY wechat_mcp /app/wechat_mcp
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd -m -u 1000 wx && mkdir -p /data && chown wx:wx /data
ENV WECHAT_MCP_HOME=/data HOST=0.0.0.0 PORT=8000 PYTHONPATH=/app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
