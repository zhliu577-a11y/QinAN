FROM node:22-bookworm-slim

# 固定版本，避免线上被自动升级打乱；升级时改这里并重新构建
ARG OPENCODE_VERSION=1.18.31
ARG NPM_REGISTRY=https://registry.npmmirror.com

# ripgrep 是 opencode 的内部依赖；git 用于项目/VCS 探测
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep tini \
 && rm -rf /var/lib/apt/lists/*

RUN npm config set registry "${NPM_REGISTRY}" \
 && npm install -g "opencode-ai@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && opencode --version

# node 官方镜像自带 UID/GID 1000 的 node 用户，直接 useradd --uid 1000 会失败。
# 这里把它改名为 app：保留 1000，命名卷首次挂载时才能继承正确的属主。
RUN usermod --login app --home /home/app --move-home --shell /bin/bash node \
 && groupmod --new-name app node \
 && mkdir -p /srv/agent/workspace /home/app/.local/share /home/app/.config \
 && chown -R app:app /srv/agent /home/app

USER app
WORKDIR /srv/agent/workspace

ENV HOME=/home/app \
    XDG_CONFIG_HOME=/home/app/.config \
    XDG_DATA_HOME=/home/app/.local/share \
    OPENCODE_SERVER_USERNAME=opencode

EXPOSE 4096

# /global/health 受 Basic 认证保护，这里带上实例自己的凭据
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS -u "${OPENCODE_SERVER_USERNAME}:${OPENCODE_SERVER_PASSWORD}" \
      "http://127.0.0.1:${PORT:-4096}/global/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "opencode"]
CMD ["serve", "--hostname", "0.0.0.0", "--port", "4096"]
