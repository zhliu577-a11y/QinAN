# 部署说明（Docker Compose）

## 1. 三个容器

| 服务 | 镜像 | 公网端口 | 说明 |
|---|---|---|---|
| `nginx` | `nginx:1.27-alpine` | 80 / 443 | 唯一对外入口，TLS 终止、限流、SSE/WS 不缓冲 |
| `gateway` | `agent-gateway:local` | 无 | 你的 FastAPI 网关，只挂在内部网络 |
| `opencode-1/2/3` | `agent-opencode:local` | 无 | 三个智能体实例，每实例一把独立密码 |

`gateway` 和 `opencode-*` 都没有 `ports:`，所以宿主机也连不上，只能通过 compose 内部网络互访。

## 2. 首次启动

```bash
cd deploy
cp .env.example .env
vim .env                      # 至少改 JWT_SECRET / 三个 OC*_PASSWORD / 域名
mkdir -p certs
# 把证书放好：certs/fullchain.pem、certs/privkey.pem
docker compose up -d --build
docker compose ps
docker compose logs -f gateway
```

业务数据全部放在 Docker 命名卷里，不需要事先建目录，也不会出现
"容器以 uid 1000 运行、bind mount 目录属主是 root、SQLite 写不进去"这类问题。
需要手工创建的只有 `certs/`（放证书）。

生成随机密码：

```bash
openssl rand -hex 24          # 分别填给 OC1/OC2/OC3_PASSWORD
```

## 3. 构建顺序（重要）

网关代码（M2/M3）已经落地，`docker compose up -d --build` 可以整体启动。
若只想先验证 agent 侧，也可以单独起实例：

```bash
docker compose up -d --build opencode-1
docker compose logs -f opencode-1
```

整体启动时 nginx 会等 `gateway` 通过健康检查（`GET /api/v1/health`）后再开放流量。

### 启动前的自检

```bash
docker compose config >/dev/null && echo "compose 语法 OK"
docker compose ps                 # STATUS 里应看到 healthy
curl -s https://agent.example.com/api/v1/health
```

### 关于虚拟环境

- **容器内不需要 venv**：容器已经隔离了 Python 版本与依赖，`gateway/Dockerfile` 直接 `pip install` 到系统环境。
- **本机开发需要 venv**：写网关代码、跑测试是在 Windows 上进行的，别污染全局 Python。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r gateway\requirements.txt
```

两份 `.dockerignore` 已排除 `.venv/`，避免把宿主的虚拟环境误拷进镜像（venv 内记录的是 Windows 绝对路径，到 Linux 下全部失效）。

### 本地跑测试

测试用 `MOCK_MODE`，不需要 opencode、不需要模型凭据、不产生费用。

```powershell
.\.venv\Scripts\python.exe -m pytest gateway
# 或在 gateway 目录下
cd gateway; ..\.venv\Scripts\python.exe -m pytest -q
```

注意 `pytest.ini` 里把夹具与测试的 event loop 都设成了 `session`：调度器与事件转发是在
`runtime` 夹具里创建的后台任务，如果测试用独立的 function 级 loop，这些任务永远不会被调度，
表现为「任务一直停在 queued」。

### 本地起网关（不容器化）

```powershell
cd gateway
$env:MOCK_MODE = "true"
$env:DATABASE_URL = "sqlite:///./gateway.db"
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

**必须单 worker**：不要加 `--workers`。实例池、调度器、事件转发都是进程内状态。

## 4. 单独验证某个实例

opencode 不对外暴露端口，调试时临时映射到本机回环即可：

```bash
docker compose exec opencode-1 \
  curl -s -u "opencode:${OC1_PASSWORD}" http://127.0.0.1:4096/global/health
```

需要看更多接口时，临时加端口映射（只在调试期用，验完删掉）：

```yaml
  opencode-1:
    ports:
      - "127.0.0.1:4096:4096"
```

## 5. 模型接入

网关只负责告诉 opencode「用哪个 provider 的哪个 model」，服务地址与密钥在实例启动时注入，
因此换服务商不必改代码。`.env` 里相关的四个变量：

```
MODEL_PROVIDER=deepseek                      # provider id（models.dev 清单里的）
MODEL_NAME=deepseek-v4-flash                 # model id
MODEL_BASE_URL=http://172.16.3.6:8589/v1      # 服务地址；留空则回退 provider 官方地址
MODEL_API_KEY=sk-xxxxxxxx                    # 该地址签发的密钥
```

service 侧的落地方式是 `opencode/config/opencode.json` 覆写 `deepseek` 这个 provider
的 `options`（`{env:...}` 是 opencode 官方支持的变量替换，密钥不进仓库）：

```json
"provider": {
  "deepseek": {
    "options": {
      "baseURL": "{env:MODEL_BASE_URL}",
      "apiKey": "{env:MODEL_API_KEY}"
    }
  }
}
```

### 对模型服务的要求

只要求 **OpenAI 兼容**，两个端点够用：

- `GET  {MODEL_BASE_URL}/models` —— 列模型
- `POST {MODEL_BASE_URL}/chat/completions` —— 对话

所以内网自建网关、`api.deepseek.com`、任何一家 OpenAI 兼容代理都能直接换成 `MODEL_BASE_URL`。

**`MODEL_NAME` 必须是该地址真实提供的 id**。写错不会在启动时报错，而是第一次调用才失败。
注意 `opencode models` 列的是 models.dev 的静态清单，**不代表你的网关真能调**；以
`/models` 的实际返回为准：

```bash
curl -s -H "Authorization: Bearer $MODEL_API_KEY" "$MODEL_BASE_URL/models"
```

### 换模型 / 换服务商

以下改动都只需要重建 opencode 实例，网关不用动：

| 场景 | 要改的地方 |
|---|---|
| 只换模型（同一地址） | `.env` 的 `MODEL_NAME` |
| 换地址 / 换密钥 | `.env` 的 `MODEL_BASE_URL`、`MODEL_API_KEY` |
| 换 provider id（如改走 `openai`、`zhipuai`） | `.env` 的 `MODEL_PROVIDER` **加** `opencode.json` 里覆写的那个 key，两处必须同名 |

最后一行是最容易踩的坑：`opencode.json` 覆写的是名叫 `deepseek` 的 provider，
若 `MODEL_PROVIDER` 改成别的 id，配置就必须同步改键名，否则覆写不生效、请求会打到官方地址。

环境变量只在容器创建时注入，`restart` 不生效，改完要重建：

```bash
docker compose up -d --force-recreate opencode-1 opencode-2 opencode-3
```

### 连通性自测

```bash
# 实例内直接跑一次，能打印 PONG 说明地址 + 密钥 + 模型 id 三者都对
docker compose exec opencode-1 \
  opencode run --model deepseek/deepseek-v4-flash 'Reply with exactly: PONG'
```

密钥统一走环境变量，不要用容器内 `opencode auth login`：配置里已显式指定 `apiKey`，会盖掉登录凭据。

## 5.1 首次开户与自测

库里没有任何账号时无法登录，先用管理接口开一个（`ADMIN_TOKEN` 必须与 `.env` 一致，
且 `/internal/*` 只允许内网来源，所以请在服务器上执行）：

```bash
curl -s -X POST https://agent.example.com/internal/users \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"换成强密码","display_name":"张三","daily_quota":30}'
```

账号由谁开户取决于身份模式：模式 A 需要运维开户；模式 B 由 App 调 `/auth/exchange` 自动开户。

接着验证全链路（把 Token 替换成上一步登录拿到的值）：

```bash
TOKEN=$(curl -s -X POST https://agent.example.com/api/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"换成强密码"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST https://agent.example.com/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-1' \
  -d '{"kind":"url","url":"https://example.com/"}'

curl -s -N "https://agent.example.com/api/v1/tasks/<task_id>/events?token=$TOKEN"
```

`MOCK_MODE=true` 时返回固定样例文本，可先确认接口、SSE 与配额都通；
切到 `false` 并配好模型凭据后才走真实 agent。

## 6. 扩容到 4 个实例

1. 复制一份 `opencode-3` 服务块，改名 `opencode-4`，换容器名、`OC4_PASSWORD`、卷名 `oc4-data`
2. 在 `gateway.environment.OPENCODE_INSTANCES` 末尾追加 `,opencode-4:4096:${OC4_PASSWORD}`
3. 在 `gateway.depends_on` 里加上 `opencode-4`
4. 在 `volumes:` 段加 `oc4-data:`，在 `.env` 加 `OC4_PASSWORD`
5. `docker compose up -d`

## 7. 证书续期

用云厂商免费证书时，到期前把新证书覆盖到 `certs/`，然后：

```bash
docker compose exec nginx nginx -s reload
```

## 8. 备份

需要备份的只有两处（都在 `deploy/data/` 下）：

- `data/gateway/gateway.db` —— 账号、任务、会话映射
- `data/workspace/` —— agent 的工作目录

实例私有数据在命名卷 `oc*-data` 里，丢了只影响 agent 的会话缓存，网关侧有摘要可重建，不必备份。

```bash
docker run --rm -v agent-gateway_oc1-data:/data -v "$PWD/backup:/backup" \
  alpine tar czf /backup/oc1-$(date +%F).tgz -C /data .
```

## 9. 常用命令

```bash
docker compose ps                 # 状态与健康
docker compose logs -f nginx      # 网关日志
docker compose restart gateway    # 改完 .env 后重启网关
docker compose down               # 停止（保留数据）
docker compose up -d --build      # 重新构建并启动
```

## 10. 国内网络注意

- 镜像构建已默认走 `registry.npmmirror.com`（opencode）与清华 PyPI 镜像（Python 包）
- 如果拉基础镜像慢，给 Docker 配国内镜像加速器
- `nginx:1.27-alpine` 与 `python:3.12-slim` 也建议走加速器
- 需要抓境外网页时，在 `.env` 里填 `FETCH_HTTP_PROXY` / `FETCH_HTTPS_PROXY`，只有 opencode 实例会用到
