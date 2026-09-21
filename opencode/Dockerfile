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

# 配置直接打进镜像，不要用只读 bind mount：
# opencode 启动时会在配置目录里写 .gitignore，只读挂载会让实例 bootstrap
# 直接抛 EROFS，导致 /session、/event 等所有依赖实例状态的接口全部 500，
# 而 /global/health 依然 200 —— 故障被健康检查掩盖。镜像层是可写的，没这个问题。
COPY --chown=app:app config /home/app/.config/opencode

USER app
WORKDIR /srv/agent/workspace

ENV HOME=/home/app \
    XDG_CONFIG_HOME=/home/app/.config \
    XDG_DATA_HOME=/home/app/.local/share \
    OPENCODE_SERVER_USERNAME=opencode

EXPOSE 4096

# /global/health 受 Basic 认证保护，这里带上实例自己的凭据。
# 但它不触发实例 bootstrap，配置有问题时照样返回 200，所以改用 /session ——
# 只有实例真正 bootstrap 成功才对 /session 返回 200，避免"假健康"。
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS -u "${OPENCODE_SERVER_USERNAME}:${OPENCODE_SERVER_PASSWORD}" \
      "http://127.0.0.1:${PORT:-4096}/session" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "opencode"]
CMD ["serve", "--hostname", "0.0.0.0", "--port", "4096"]
