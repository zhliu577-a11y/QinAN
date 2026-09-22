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

参数传域名或 IP 都行（脚本按格式自动写进 SAN）。自签证书会让客户端报不受信任，
仅用于演练，生产环境务必换成正式证书。

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

### 重新部署 gateway 曾经会把整站打挂（已修）

nginx 侧原本写的是 `upstream gateway_up { server gateway:8080; }`。
`upstream` 里的主机名**只在 nginx 启动时解析一次，之后永久缓存**；而
`docker compose up -d` 重建 gateway 容器会分配新的 IP，于是 nginx 一直去连那个
已经不存在的旧地址，**每个请求都 502**，直到 nginx 也被重建。

实测日志（gateway 从 `172.31.1.5` 变成 `172.31.1.7` 之后）：

```
connect() failed (111: Connection refused) while connecting to upstream,
request: "POST /api/v1/auth/token HTTP/2.0", upstream: "http://172.31.1.5:8080/api/v1/auth/token"
```

症状很有迷惑性：`docker compose ps` 里 gateway 是 healthy，容器内
`curl http://gateway:8080/api/v1/health` 也是 200，只有经过 nginx 才 502。

已改成「变量 + resolver」，名字按 `valid=10s` 重新解析，网关重建后最多 10 秒自愈：

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;
set $gateway_up http://gateway:8080;
location ... { proxy_pass $gateway_up; }
```

代价是失去 upstream 的 keepalive（每个请求新建一条连接），对这个规模可以忽略。
注意 `127.0.0.11` 是 Docker 内置 DNS —— 如果哪天把 nginx 移出容器、直接在宿主机上跑，
要把这个地址改成宿主机 `/etc/resolv.conf` 里的 DNS，否则解析失败会全站 502。

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
MODEL_PROVIDER=deepseek                  # provider id（models.dev 清单里的）
MODEL_NAME=deepseek-v4-flash             # model id
MODEL_BASE_URL=https://api.deepseek.com/v1   # 服务地址；留空则回退 provider 官方地址
MODEL_API_KEY=sk-xxxxxxxx                # 该地址签发的密钥
```

`MODEL_BASE_URL` 必须兼容 OpenAI 协议：`GET /v1/models` 列模型、
`POST /v1/chat/completions` 对话（支持 `stream: true`）。自建网关与服务商官方地址都行。

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

所以自建网关、`api.deepseek.com`、任何一家 OpenAI 兼容代理都能直接换成 `MODEL_BASE_URL`。

**`MODEL_NAME` 必须是该地址真实提供的 id**。写错不会在启动时报错，而是第一次调用才失败。
注意 `opencode models` 列的是 models.dev 的静态清单，**不代表你的网关真能调**；以
`/models` 的实际返回为准：

```bash
curl -s -H "Authorization: Bearer $MODEL_API_KEY" "$MODEL_BASE_URL/models"
```

### 没有模型服务时怎么验证（模拟模型）

要在**完全不依赖任何外部模型**的前提下把整条链路跑通、并且能反复回归，
用自带的模拟模型（`.env.example` 默认就指向它，`docker compose up -d` 会一起拉起）：

```
MODEL_BASE_URL=http://mock-model:8000/v1
MODEL_API_KEY=mock-key-not-used
```

如果是从真实模型切回来，重建实例让变量生效：
`docker compose up -d --force-recreate opencode-1 opencode-2 opencode-3`。

`deploy/mock-model/` 是 OpenAI 兼容的假模型，**只用 python 标准库**（构建不装依赖，
断网也能构建）。它的行为是确定性的，所以可以做自动化断言：

- 提示词里有 URL 且工具含 `webfetch` → 先回一个 `webfetch` 工具调用，
  让 opencode 真的去抓网页，从而把工具调用与抓取元信息那条路径也走到
- 已经拿到工具结果 → 回最终摘要
- 其余 → 直接按输入文本回一段结构化摘要

输出以 `【模拟模型】` 开头，一眼能看出不是真实模型。它不做鉴权、内容是编造的，
**绝不能用于生产**。它不映射端口、只挂在内部网络上，所以默认随 `up -d` 一起启动
也不会扩大暴露面；接上真实模型后想省资源可以 `docker compose stop mock-model`。

> 为什么不把它藏进 compose profile：`.env.example` 的 `MODEL_BASE_URL` 默认指向它，
> 而首次启动的命令是 `docker compose up -d --build`（不带任何 profile）。两者一错位，
> opencode 就连不上模型，每个任务都会失败，而且要到第一次提交任务才暴露。

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

### 模拟覆盖不到的东西（别被"全绿"骗了）

默认配置走模拟模型时，下面这些**一定是好的**，别浪费时间验证：
消息链路、排队与派发、SSE 增量、任务状态流转、取消、配额、`source` 字段（含
`fetched_chars` 的三种取值）、回调签名与重试、多实例分配。

但下面这些**模拟下证明不了**，上线前必须在真实模型上过一遍：

| 模拟下测不出来的 | 原因 | 上线前怎么确认 |
|---|---|---|
| **模型凭据 / 地址是否可用** | mock-model 不校验 `MODEL_API_KEY`，写错也能过 | 做一次「连通性自测」，或在真实模型上提交一个任务 |
| **`usage` 里的 tokens 与 cost** | 数字是按真实单价乘**编造的** token 数，`cost` 看着很像真的却纯属虚构 | 只能以真实模型返回为准，**不要拿它做预算或计费** |
| **任务超时回收（180s）** | 模拟任务 1~2 秒就结束，永远到不了超时 | 临时调小 `TASK_TIMEOUT_SECONDS` 跑一次，或等真实长任务 |
| **抓取失败的处理** | 模拟下 webfetch 永远成功 | 用一个必定失败的 URL（如不存在的域名）验证 `fetched_chars` 为 `null` |
| **排队积压与并发** | 模拟太快，压不出队列 | 用真实模型跑 20 人并发，见「容量实测」一节 |
| **模型多轮工具调用** | mock 只做一轮（先抓一次，再给摘要） | 真实模型可能连抓多页，需观察 `handle_fetch` 的累加 |

还有一个常见误判：`/api/v1/health` 返回 `engine: ready` **不代表模型可用**。
网关只探 opencode 实例的活性，不探模型。mock-model 挂掉时 opencode 依旧 healthy，
要等第一个任务失败才暴露。所以 `docker compose ps` 里请确认 `agent-mock-model`
也是 `healthy`。

## 6. 首次开户与自测

库里没有任何账号时无法登录，先用管理接口开一个。`ADMIN_TOKEN` 必须与 `.env` 一致。

**管理接口不从公网入口暴露**：`/internal/*` 在 nginx 里是 `deny all`，所以
`https://域名/internal/...` 一定 403。请直接打到 `gateway` 容器（它只在 internal
网络里，镜像内自带 `curl`）：

```bash
docker compose exec gateway curl -s -X POST http://127.0.0.1:8080/internal/users \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"换成强密码","display_name":"张三","daily_quota":30}'
```

> 为什么用 `deny all` 而不是 nginx 白名单：`deny all` 是无条件的，不依赖来源 IP，
> 所以在任何网络模式下都成立。而白名单要为「最终是哪个地址进来」负责，那件事
> 该由防火墙/安全组定义；在 nginx 里再写一份，线上换入口（加 LB、改 host 网络）
> 时容易和实际脱节，看起来拦住了其实没有。

日常运维（看状态、翻日志、重启）都不用记这些细节，直接用 `deploy/ops.sh`，见第 14 节。

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

## 7. 两层模拟，别搞混

「模拟」在这个项目里有两个层次，作用完全不同：

| 开关 | 走的路径 | 用途 |
|---|---|---|
| `MOCK_MODE=true` | 网关直接造假 agent，**不启动 opencode** | App 侧快速对接口、SSE、配额；不需要 opencode 与模型 |
| `MOCK_MODE=false` + `MODEL_BASE_URL` 指向 `mock-model` | 网关 → opencode → mock-model | 验证整条链路（默认配置） |

两者的差别不只是快慢：`MOCK_MODE=true` 时 opencode 与 mock-model 都在空转，
**webfetch 根本不会发生**，所以 url 任务的 `fetched_chars` 恒为 `null`。
要验证抓取与 `source` 字段就必须用 `MOCK_MODE=false`。

## 8. 扩容到 4 个实例

1. 复制一份 `opencode-3` 服务块，改名 `opencode-4`，换容器名、`OC4_PASSWORD`、卷名 `oc4-data`
2. 在 `gateway.environment.OPENCODE_INSTANCES` 末尾追加 `,opencode-4:4096:${OC4_PASSWORD}`
3. 在 `gateway.depends_on` 里加上 `opencode-4`
4. 在 `volumes:` 段加 `oc4-data:`，在 `.env` 加 `OC4_PASSWORD`
5. `docker compose up -d`

## 9. 证书续期

用云厂商免费证书时，到期前把新证书覆盖到 `certs/`，然后：

```bash
docker compose exec nginx nginx -s reload
```

## 10. 备份

真正需要备份的只有两处，都在 **Docker 命名卷**里（不是 `deploy/data/` 目录，
那里只有 nginx 的日志与 certbot 的临时文件）：

- 卷 `gateway-data` → 容器内 `/srv/agent/data/gateway.db` —— 账号、任务、会话映射
- 卷 `workspace` → 容器内 `/srv/agent/workspace` —— agent 的工作目录与抓取产物

实例私有数据在命名卷 `oc*-data` 里，丢了只影响 agent 的会话缓存，
网关侧有摘要可重建，不必备份。卷名前缀是 compose 的 project name（`agent-gateway_`），
可用 `docker volume ls` 确认实际名字。

```bash
mkdir -p backup
for vol in gateway-data workspace; do
  docker run --rm -v "agent-gateway_${vol}:/data:ro" -v "$PWD/backup:/backup" \
    alpine tar czf "/backup/${vol}-$(date +%F).tgz" -C /data .
done
ls -lh backup/
```

恢复前先 `docker compose stop gateway`，把包解回同名卷后再 `up -d`，
避免 SQLite 在写入过程中被复制出半截状态。

## 11. 常用命令

日常运维优先用 `./ops.sh`（见第 14 节），它把下面这些容易记错的细节都固化好了。
直接敲 docker 命令时的注意事项：

```bash
docker compose ps                             # 状态与健康
docker compose logs -f gateway                # 网关日志（stdout 是权威副本）
docker compose up -d --force-recreate gateway # 改完 .env 后重建网关
docker compose down                           # 停止（保留数据）
docker compose up -d --build                  # 重新构建并启动
```

两个坑：

- 改 `nginx.conf` 后**不能用 `restart`**：它是单文件 bind mount，`restart` 只会重载
  容器创建时那个 inode，看不到新内容。必须 `--force-recreate nginx`。
- 改 `.env` 后也要 `--force-recreate`，`restart` 不会重新读取。

## 12. 国内网络注意

- 镜像构建已默认走 `registry.npmmirror.com`（opencode）与清华 PyPI 镜像（Python 包）
- 如果拉基础镜像慢，给 Docker 配国内镜像加速器
- `nginx:1.27-alpine` 与 `python:3.12-slim` 也建议走加速器
- 需要抓境外网页时，在 `.env` 里填 `FETCH_HTTP_PROXY` / `FETCH_HTTPS_PROXY`，只有 opencode 实例会用到

## 13. 容量实测（3 实例 / 4 vCPU 虚拟机）

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

### 限流按凭证（token）分桶

`nginx.conf` 里的限流键是 `$rl_key`：带 `Authorization: Bearer ...` 的请求按 token 分桶，
一人一个桶、互不挤占；没带凭证的请求（登录、健康检查、`/openapi.json`）退回按来源 IP 分桶。

| 桶 | 键 | 速率 | 位置 |
|---|---|---|---|
| `rl_general` | token / 来源 IP | 120 r/m，burst 40 | `/api/v1/` 下的查询、`/api/v1/ws/` |
| `rl_submit` | token / 来源 IP | 120 r/m，burst 30 | `POST /api/v1/tasks` |
| `rl_sse` | token / 来源 IP | 60 r/m，burst 20 | `GET /api/v1/tasks/{id}/events` |
| `rl_auth` | 来源 IP | 20 r/m，burst 10 | `POST /api/v1/auth/{token,exchange,refresh}` |

三点必须知道的前提：

- **按凭证分桶的前提是「一人一个 token」。** 如果 App 后端把所有用户的请求
  都用同一个 service token 转发，那这些用户仍然会被算成同一个人、互相挤占额度。
  这时要么给每个终端各自换取 token（推荐，模式 B 的 `/auth/exchange` 就是为此设计的），
  要么同步调大 `rl_submit`。
- **重新登录 / 换 Token 会拿到一个新桶**，等于把自己的额度重置。这条路径需要凭据
  才能走，所以重置次数被「能拿到多少凭据」限住，不构成绕过。
- **别拿本机压测的 429 比例推断线上行为。** 实测：外部客户端的来源 IP 是保留的
  （宿主机 curl 经 `127.0.0.1:8443` 进来，`access.log` 记到 `10.0.2.2`），
  但从本机经发布端口发起的请求会被 docker-proxy/DNAT 折叠成网桥网关 `172.31.1.1`，
  所有本机流量共用一个桶。在 VM 里自测高并发时，看到的是被折叠后的结果。

**429 有两种来源，App 必须分开处理：**

- 网关返回的 429：JSON body `{"error":{"code":"QUOTA_EXCEEDED"|"QUEUE_FULL"|"RATE_LIMITED",...}}`，
  带 `Retry-After`，可以按 `code` 给用户不同文案。**没有** `X-RateLimit-*` 头。
- nginx 返回的 429：body 是 nginx 的 HTML 错误页，**没有** `Retry-After`。
  见到这种就只能按固定间隔退避重试，不要拿它当业务错误展示。

### 轮询 vs SSE：结论没变，但理由变了

`GET /api/v1/tasks/{id}` 走 `rl_general`，**App 应该用 `GET /api/v1/tasks/{id}/events` 拿进度**。

理由要更新一下，别照抄旧说法。以前的结论是「20 个客户端每秒轮询一次 = 1200 次/分钟，
会被限流挡掉」，那是**旧的全局限流桶**下的结果。按凭证分桶之后，单个用户
1 秒轮询 1 次只有 60 r/m，低于 `rl_general` 的 120 r/m，**不会**必然被拒。

轮询真正还会踩到的两种情况：

- **App 后端用同一个 token 转发所有用户**：等价于旧的全局桶。那时 20 秒 400 次请求里
  被拒 154 次、连 SSE 都被挤成 429 的场景会原样重演（这个数字是实测踩出来的，不是推算）。
- 轮询本身的问题不只在限流：它拿到的进度粒度更粗、更费电，而且和 SSE 相比要多一次
  建连开销。这是设计取舍，不是服务端能兜住的。

SSE 有自己的 `rl_sse`（60r/m burst 20）桶，不会被查询流量挤占。

## 14. 运维：状态 / 日志 / 重启

统一入口是 `./ops.sh`（在 `deploy/` 下，跑在宿主机上）：

```bash
./ops.sh status                  # 容器 + 对外健康 + 网关自检，先看这个
./ops.sh health                  # 只输出 ok/degraded，退出码即状态，给监控用
./ops.sh instances               # 后端 opencode 进程明细
./ops.sh logs gateway -n 100     # 容器 stdout 日志
./ops.sh logs-api --level error  # 网关进程自己的近期日志
./ops.sh user zhangsan           # 单个用户详情
./ops.sh restart gateway         # 重建（不是 restart，原因见下）
./ops.sh heal                    # 只重建不健康的容器
./ops.sh backup                  # 备份数据卷
./ops.sh help                    # 全部命令
```

### 设计：为什么重启和别人的日志不在接口里

`/internal/` 只覆盖网关**自己**的进程内状态。容器层的东西（重启、nginx /
opencode 的 stdout）刻意留给宿主机上的 `ops.sh`，因为要让容器做这些事就得给它挂
docker socket —— 挂上 socket 等价于把宿主机 root 交出去（可以起特权容器挂 `/`），
为了几个运维按钮不值得。这个边界是刻意的，不要为了「统一入口」去破它。

### 状态类内部接口

都在 `gateway` 容器内可达，需要 `X-Admin-Token`，从公网一律 403：

| 接口 | 用途 |
|---|---|
| `GET /internal/status` | 一屏自检：`checks` 里每项带 `ok` 与 `detail`，`status` 是总判定；另含 `uptime_seconds`、池子、队列、关键配置 |
| `GET /internal/instances` | 逐个 opencode 进程：`status` / `current_task_id` / `busy_seconds` / `failures` / `sessions` |
| `GET /internal/logs` | 网关进程近期日志（环形缓冲，默认 2000 条，重启清空） |
| `GET /internal/metrics` | 池与队列的聚合数字 |
| `GET /internal/users` | 用户列表 |
| `GET /internal/users/{id}` | 单用户详情：配额用量、任务分布、近 10 条任务、登录设备、实例绑定 |

`/internal/logs` 支持 `limit`、`level`、`logger_name`、`task_id`、`after_seq`。
`after_seq` 用来增量拉取：把上次拿到的最大 `seq` 传进去，只取新增部分。

排查顺序建议：`status` 看总判定 → `instances` 看是不是某个进程卡住 →
`logs-api --level error` 看网关报了什么 → `logs <服务>` 看容器 stdout。

> `/internal/logs` 只覆盖网关，且只在内存里。日志的权威副本仍然是容器 stdout，
> 由 docker 收集（`./ops.sh logs`）。别把环形缓冲当成日志归档。

### 重启语义（重要）

网关是**单 worker、状态全在进程内**（实例池、调度器、事件转发），所以重建它会打断
正在执行的任务。为此启动时会做一次回收：

- 库里停在 `running` / `streaming` 的任务，如果还没重试过 → 放回队列重试一次；
- 已经重试过的 → 判 `failed`，错误码 `GATEWAY_RESTARTED`。

所以重启不会让任务永久卡在「进行中」，客户端一定能等到终态。代价是那一次重试会
在新实例上重建上游会话，前一次的部分输出作废 —— 摘要任务很短，这个取舍可以接受。

重建命令一律走 `up -d --force-recreate` 而不是 `restart`：`nginx.conf` 是单文件
bind mount，`restart` 只会重载容器创建时那个 inode；`.env` 的改动同样只有重建才生效。
`ops.sh restart` 已经按这个来了。

### 健康探针分层

四个容器都有健康检查，但探的东西**故意不一样**：

| 容器 | 探针 | 探的是什么 |
|---|---|---|
| `gateway` | `GET /api/v1/health` | 网关自己 + 数据库 + 引擎状态 |
| `nginx` | `GET /healthz`（明文 80） | nginx 自己，**不经过 gateway** |
| `opencode-N` | `GET /session`（带 basic auth） | 单个 agent 实例能否应答 |
| `mock-model` | `GET /health` | 模拟模型进程 |

nginx 的探针刻意不代理到 gateway：否则网关一挂就会把 nginx 也判成不健康，
`ops.sh heal` 会去重建一个本来没问题的容器。也不走 TLS，省掉自签证书的校验问题。

`/healthz` 在 80 与 443 都注册了（返回 `ok`，无任何信息泄露），云上的负载均衡
也可以直接用它。

### 模式 B（App 侧可信直传）

App 侧已有用户体系时走这条路：App 后端用共享密钥签名换 Token，用户不需要二次登录。

```bash
# 1) 生成密钥（与 App 团队约定同一个值，只放服务端）
openssl rand -hex 32

# 2) 填进 deploy/.env 的 EXCHANGE_HMAC_SECRET，然后重建
./ops.sh restart gateway

# 3) 自检：密钥没配会返回 403「未启用 App 侧可信直传」；
#    配了但签名不对会返回 401「签名校验失败」
```

密钥为空时 `/auth/exchange` 直接 403，这是**预期行为**（模式 B 默认关闭），
不是故障。签名算法与注意事项见 `docs/API.md` 第 2.2 节。
