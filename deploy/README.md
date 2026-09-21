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
# 把正式证书放好：certs/fullchain.pem、certs/privkey.pem
# 本地演练没有域名怎么办 → 见下面「没有域名时怎么本地演练」
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

### 拉取镜像（境内必做）

Docker Hub 在境内直连不通（实测 `registry-1.docker.io` 请求超时）。不配加速器，
`docker compose up --build` 会一直卡在拉取 `nginx` / `python` / `node` 三个基础镜像上。

编辑 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": ["https://docker.m.daocloud.io"]
}
```

```bash
sudo systemctl restart docker
docker info | grep -A2 'Registry Mirrors'     # 确认已生效
```

`https://docker.m.daocloud.io` 实测可取到本项目用到的三个基础镜像。
若有阿里云账号，可在控制台「容器镜像服务 → 镜像加速器」拿到专属地址，两者可并列写进数组。

镜像内的依赖下载已默认走国内源（`opencode/Dockerfile` 用 `registry.npmmirror.com`，
`gateway/Dockerfile` 用清华 PyPI），不需要额外配置。

### 没有域名时怎么本地演练

`nginx.conf` 强制 TLS：证书文件不存在时容器会直接启动失败，且 80 端口只做 301 跳转到 443。
在没有域名、没有正式证书的 VM 里试跑，用自签证书：

```bash
chmod +x ./gen-self-signed-cert.sh      # 从 Windows 提交过来时可能没有执行位
./gen-self-signed-cert.sh 192.168.1.50 agent.example.com
docker compose up -d --build
```

参数传域名或 IP 都行（脚本按格式自动写进 SAN）。手机首次访问需手动信任该证书，
自签证书仅用于演练，生产环境务必换成正式证书。

要让同网段的手机连上演练环境，还得在宿主机放行转发端口（Windows 为例，
管理员 PowerShell 执行一次即可）：

```powershell
New-NetFirewallRule -DisplayName 'QinAN VM gateway HTTP 8080' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080 -Profile Any
New-NetFirewallRule -DisplayName 'QinAN VM gateway HTTPS 8443' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8443 -Profile Any
```

然后手机浏览器访问 `https://<宿主机局域网IP>:8443/api/v1/health`，能返回
`{"status":"ok",...}` 就说明链路通了。注意 80 端口只做 301 跳转，所以要访问
443 对应的那个映射端口（示例里是 8443）。

另外，宿主机上如果有别的进程占着 `127.0.0.1:8080`，本机用 `127.0.0.1` 测会打到
那个进程而不是 VM（VirtualBox 的 NAT 监听绑在 `0.0.0.0`，更具体的 `127.0.0.1`
绑定优先）。这种情况下用宿主机的局域网 IP 测即可，手机访问不受影响。

## 3. 构建顺序（重要）

网关代码（M2/M3）已经落地，`docker compose up -d --build` 可以整体启动。
若只想先验证 agent 侧，也可以单独起实例：

```bash
docker compose up -d --build opencode-1
docker compose logs -f opencode-1
```

整体启动时 nginx 会等 `gateway` 通过健康检查（`GET /api/v1/health`）后再开放流量。

### 改完 nginx.conf 记得重建容器

`nginx.conf` 是以**单文件** bind mount 挂进容器的。改完之后只执行
`docker compose exec nginx nginx -s reload` 是**没用的**：reload 只是重新读取
容器里挂着的那个 inode，而 git pull 与编辑器保存都是「写新文件再 rename」，
旧 inode 已被 unlink，于是它一直读旧内容——`nginx -T` 会显示旧值，
但 `nginx -t` 仍然报 successful，很容易误判成已经生效。

配置改动必须重建容器：

```bash
docker compose up -d --force-recreate nginx
```

只跑 `docker compose up -d` 也不够：挂载文件的内容变化不在 compose 的配置哈希里，
它不会认为需要重建。改完可以用这条确认容器里读到的就是新文件：

```bash
sha256sum nginx.conf                                        # 宿主机
docker compose exec nginx sha256sum /etc/nginx/nginx.conf   # 容器内，两者应一致
```

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

## 11. 容量实测（3 实例 / 4 vCPU 虚拟机）

20 个用户同时提交、真实模型（非 MOCK）实测：

| 指标 | 实测值 |
|---|---|
| 提交成功率 | 20 / 20 |
| 全部完成耗时 | 18.7s |
| 吞吐 | ≈ 1.07 任务/秒 |
| 单任务平均耗时 | 2.3s |
| 实例分布 | oc-1 7 个、oc-2 7 个、oc-3 6 个 |

吞吐基本被模型时延锁死：单任务约 2.3s，3 个实例并行，理论上限约 3/2.3 ≈ 1.3 任务/秒，
实测 1.07 与之相符。所以要提高并发能力，**加实例是线性有效的**，
瓶颈不在网关也不在 nginx。按每人每天提交几次算，3 个实例对 20 人绰绰有余。

要注意队列上限：20 个任务里有 17 个排在队列里等实例。业务侧的
`MAX_QUEUE_DEPTH_PER_USER=3` 只限制单个用户，全局队列没有上限，
极端情况下（几十人同时提交）会让后来者等很久。真出现这种情况，
优先加实例，或者给全局队列加一个上限并在超限时返回明确的忙时错误。

### 提交接口是按 IP 限流的

`nginx.conf` 里 `ip_submit` 按 `$binary_remote_addr` 限流，不是按用户。
如果 App 后端做代理转发，或者所有手机都在同一个出口 NAT 后面，
这些用户会被算成「同一个 IP」而互相挤占额度。当前配置是
`rate=120r/m burst=30`，够同一 IP 后 20 人同时提交；
但如果你的 App 是这种架构、并且并发会更高，记得同步调大这个值。

另外 `/api/v1/` 下的查询接口共用 `ip_general`（120r/m burst 40）。
App 应该用 SSE 拿进度而不是轮询任务状态：20 个客户端每秒轮询一次就是
1200 次/分钟，会被限流挡掉。SSE 的设计路径不受影响。
