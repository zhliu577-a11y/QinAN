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

如果是从真实模型切回来，改完 `.env` 跑 `docker compose up -d` 落地（不要点名服务，
原因见下面「换模型 / 换服务商」）。

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

**模型这件事被切成了两半，分别由网关和 opencode 决定**，所以「改完重建谁」取决于你改的是哪一半：

| 谁决定 | 决定什么 | 读哪些变量 |
|---|---|---|
| `gateway` | **问哪个模型**：每个任务下发 `{providerID, modelID}` | `MODEL_PROVIDER`、`MODEL_NAME`、`SUMMARIZER_AGENT` |
| `opencode` | **问到哪、用什么密钥**：`baseURL` / `apiKey` | `MODEL_BASE_URL`、`MODEL_API_KEY` |

因此：

| 场景 | 要改的地方 | 重建谁 |
|---|---|---|
| 只换模型（同一地址） | `.env` 的 `MODEL_NAME` | **gateway + opencode** |
| 换地址 / 换密钥 | `.env` 的 `MODEL_BASE_URL`、`MODEL_API_KEY` | opencode |
| 换 provider id（如改走 `openai`、`zhipuai`） | `.env` 的 `MODEL_PROVIDER` **加** `opencode.json` 里覆写的那个 key，两处必须同名 | gateway + opencode（改 `opencode.json` 还要 `--build`） |

**「只换 `MODEL_NAME` 时只重建 opencode」是错的**——这是实测确认过的坑，见下。
`MODEL_PROVIDER` / `MODEL_NAME` 网关自己也读了一份（`gateway/app/core/config.py` 的
`model_provider` / `model_name`），由 `dispatcher` 在派发时逐个任务下发
（`gateway/app/services/dispatcher.py` 的 `provider_id=` / `model_id=`）。

> **实测记录**：把 `.env` 的 `MODEL_NAME` 改成 `probe-ctl-X`，然后执行
> `docker compose up -d --force-recreate opencode-1 opencode-2 opencode-3`：
> gateway 容器 **ID 完全没变**（`5589e18208c6`，StartedAt 也没变），进程内
> `MODEL_NAME` 仍是 `deepseek-v4-flash`，`/internal/status` 的 `config.model_name`
> 也是旧值；提交任务后 mock-model 收到的请求是 `model=deepseek-v4-flash`。
> 也就是说：**网关会一直用它启动时那份配置去问模型，改了 `.env` 也照样发旧的 model id。**
> 配合真实服务商时，症状是「换了模型但账单/行为毫无变化」，而且不报任何错。

**这个坑的根因不是「compose 不会检测变化」，而是那条命令点名了服务。**
`up -d` 后面一旦点名服务，compose 就只处理点到的那几个；没点名的即使配置变了也不动。
不点名地跑 `docker compose up -d` 时，compose 会把每个服务的 `env_file` 内容算进配置
哈希，**精确重建真正受影响的服务**。实测（改不同变量后跑不点名的 `up -d`）：

| 只改这个变量 | 谁被重建 | 谁没动 |
|---|---|---|
| `DEFAULT_DAILY_QUOTA`（只有 gateway 读） | gateway | opencode-1/2/3、nginx |
| `MODEL_BASE_URL`（opencode 读，但它是 `.env` 的一部分） | opencode-1/2/3 **和 gateway** | nginx |
| `OC1_PASSWORD` | oc-1 **和 gateway** | oc-2、oc-3、nginx |

注意第二、三行：因为 gateway 用 `env_file: .env` 吃整份文件，**改 `.env` 里任何一个
值都会让 gateway 被重建**。这听着浪费，但正是它保证了 gateway 永远不会停留在旧配置上。

另外，`MODEL_PROVIDER` 与 `opencode.json` 的键名必须同名：`opencode.json` 覆写的是名叫
`deepseek` 的 provider，若 `MODEL_PROVIDER` 改成别的 id，配置就要同步改键名，
否则覆写不生效、请求会打到官方地址。

环境变量只在容器创建时注入，`docker compose restart` 不生效。改完 `.env` 要这样落地：

```bash
docker compose up -d              # 不点名：compose 自己算出该重建谁
# 或者等价的封装（会顺带等服务健康）
./ops.sh apply
```

**不要点名**，也不要加 `--force-recreate`：`--force-recreate` 只在你确实想「无条件重启
这几个」时用（比如改完 `nginx.conf`）。改 `.env` 时加它反而会绕过 compose 的变化检测。

> 重建实例后 **前 10~30 秒不要提交任务**：池子的健康检查每 10 秒一轮、连续 3 次失败才
> 标记不可用，所以这段时间里网关仍会往刚重启、还没 bootstrap 完的实例派活。
> 实测到的报错是 `创建会话失败 instance=oc-1: All connection attempts failed`，
> 任务直接 `failed`。先 `docker compose ps` 等六个容器都 healthy 再开始用。

### 连通性自测

分两层查，先确认「模型通不通」，再确认「整条链通不通」。

**第一层：从实例内部直接打模型端点**（不经过网关，能定位问题在模型侧还是链路侧）：

```bash
# 1) 地址通不通、这个地址上有哪些模型
docker compose exec opencode-1 sh -c \
  'curl -s -H "Authorization: Bearer $MODEL_API_KEY" "$MODEL_BASE_URL/models"'

# 2) 用你配置的 model id 真的问一句（把 deepseek-chat 换成你的 MODEL_NAME）
docker compose exec opencode-1 sh -c \
  'curl -s -X POST "$MODEL_BASE_URL/chat/completions" \
     -H "Authorization: Bearer $MODEL_API_KEY" -H "Content-Type: application/json" \
     -d "{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: PONG\"}]}"'
```

第 2 条能回内容，就说明**地址 + 密钥 + 模型 id 三者都对**。

> **注意**：对着 `mock-model` 跑这两条时，密钥写错也照样返回 200（它不鉴权）。
> 所以「密钥是否有效」这一项只有接真实服务商才能真正验证——那时密钥错会返回 401。

**第二层：走完整链路提交一个真实任务**（这才是最终判据，覆盖网关 + opencode + 模型）：

```bash
TOKEN=$(curl -s -X POST https://agent.example.com/api/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"强密码"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST https://agent.example.com/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: selftest-1' \
  -d '{"kind":"text","text":"连通性自测","instruction":"一句话总结"}'

# 拿返回的 task_id 查结果：status=succeeded 且 result_md 有内容即通过
curl -s https://agent.example.com/api/v1/tasks/<task_id> -H "Authorization: Bearer $TOKEN"
```

> **不要用 `docker compose exec opencode-1 opencode run --model ... '...'` 做自测**：
> 实测这个命令在非 TTY（脚本、ssh、CI）下会**挂住不返回**——`timeout` 到点被杀，
> 退出码 124、一行输出都没有；而 opencode 自己的日志显示模型调用其实成功了
> （`message=stream ... llm runtime selected`）。请求确实到了模型端，是 CLI 不吐结果，
> 拿它判断「模型配错了」会直接误判。上面第一层的 curl 是等价且可靠的替代。

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

## 8. 扩容到更多实例

实例数量就是 `docker-compose.yml` 里 `opencode-N` 服务块的个数，**没有自动扩缩容**。
加一个实例要同步改四处，少改哪一处的后果都不同：

| # | 改哪里 | 漏掉的后果 |
|---|---|---|
| 1 | 复制一份 `opencode-3` 服务块 → `opencode-4`，改 `container_name`、`OPENCODE_SERVER_PASSWORD`、卷名 `oc4-data` | 容器起不来 |
| 2 | `gateway.environment.OPENCODE_INSTANCES` 末尾追加 `,opencode-4:4096:${OC4_PASSWORD}` | **容器起来了、也是 healthy，但网关不知道它存在，永远不派活**——最隐蔽的一种 |
| 3 | `gateway.depends_on` 加上 `opencode-4` | 网关照常在实例就绪前起来，开头几十秒的任务失败 |
| 4 | 顶层 `volumes:` 加 `oc4-data:`；`.env` 加 `OC4_PASSWORD` | compose 报卷未定义 / 实例密码为空起不来 |

两条实测踩到的坑：

- **第 2 步不要用「把 `${OC3_PASSWORD}` 替换掉」这种改法**：这个占位符在文件里出现两次
  （第 90 行的实例名册、第 139 行的 `OPENCODE_SERVER_PASSWORD`），按占位符替换很容易改错那一处，
  结果名册没变、实例照样不被派活。要锚定整行的 `opencode-3:4096:${OC3_PASSWORD}` 再追加。
- **新实例不要写 `build:`**。三个实例共用 `agent-opencode:local` 这个 tag，多处写 `build`
  会并行构建抢写同一个 tag，报 `image agent-opencode:local already exists`。
  新块只用 `image:`，镜像已经在了。

改完：

```bash
docker compose config >/dev/null && echo "语法 OK"   # 先验语法
docker compose up -d                                 # 不需要 --build，复用现成镜像
./ops.sh instances                                   # 名册里要能看到 oc-4
```

`./ops.sh instances` 是**唯一能证明扩容真的生效**的检查。`docker compose ps` 里
`agent-opencode-4` 是 healthy 只说明容器活着，不代表网关认得它。

实测（3 → 4）：7 个容器全部 healthy，`/internal/instances` 返回
`{"total": 4, "idle": 4, "healthy": 4}`；用 4 个用户各提交 1 条并发任务，
4 个实例都被观察到 `busy`，4 条任务全部 `succeeded`。
实例 id 是按 `OPENCODE_INSTANCES` 里的**顺序**生成的（`oc-1`、`oc-2`…），
顺序决定 id，与容器主机名无关。

`NO_PROXY` 里逐个列出的实例名不用跟着改：实例之间不互相调用，那几个名字是防御性写的。
`./ops.sh restart opencode` 会自己从 compose 读出实例列表，新实例也能一起重建。

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

### 宿主机重启后自动恢复

容器本身有 `restart: unless-stopped`，虚拟机内 `docker.service` 也是 enabled，
所以**只要虚拟机起来了，六个容器会自己回来**。

缺的一环是虚拟机本身：VirtualBox 的 autostart 需要 `VBoxAutostartSvc` 服务，
它默认不装，安装需要管理员权限。当前用的是**登录触发的计划任务**（不需要管理员）：

```powershell
Get-ScheduledTask -TaskName 'QinAN demo VM (headless)' |
  Select-Object TaskName, State
Start-ScheduledTask -TaskName 'QinAN demo VM (headless)'   # 手动拉起来
```

实测：计划任务触发 → 26 秒 SSH 可用 → 47 秒网关 `health` 返回 `ok`。

> **注意这个方案的边界**：登录触发只在**有人登录之后**才生效。宿主机无人值守重启后
> 虚拟机不会自己起来。要做到真正与登录无关，必须以管理员身份装
> `VBoxAutostartSvc` 并配 `autostart.cfg`（或把计划任务改成「计算机启动时」+ 以
> SYSTEM 运行）。这是当前唯一需要管理员权限才能补的运维项。

## 15. 真实服务器部署清单

前面各节讲的是单个环节怎么弄，这一节是**从一台空机器到手机能用的完整顺序**。
每一步都配了验证命令，验不过别往下走——这个项目里「容器 healthy」跟「真的能用」
是两件不同的事（模型配置写错、名册漏改、`engine: ready` 都踩过这个坑）。

### 15.1 动手前先定下来的事

| 项 | 建议 | 说明 |
|---|---|---|
| 服务器 | 4 vCPU / 4 GB 起步，**推荐 8 GB**，至少加 2 GB swap | 每个 opencode 实例是一个 Node 进程；4 GB 跑 3 个实例没有 swap 会吃紧 |
| 系统盘 | ≥ 40 GB | 镜像 2.5 GB + 构建缓存 1.3 GB + 数据卷 |
| 系统 | Ubuntu 22.04 / 24.04 | 演示环境就是 24.04，其他发行版未验证 |
| 域名 | **已备案**（境内服务器） | 未备案域名走 80/443 会被拦，只能改用高位端口 |
| 安全组 | 只放行 80、443（加你的 SSH 端口） | 其余一律不放开：gateway 与 opencode 都没有映射端口，不需要 |
| TLS 证书 | 正式证书 → `certs/fullchain.pem`、`certs/privkey.pem` | `nginx.conf` 强制 TLS，证书缺失时 nginx 直接启动失败 |

### 15.2 装 Docker，配镜像加速器（境内必做）

```bash
sudo apt-get update && sudo apt-get install -y git
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker    # 免 sudo；重登一次才彻底生效
docker compose version                              # 需要 v2，命令是 docker compose（不是 docker-compose）
```

`registry-1.docker.io` 在境内直连超时（实测），不配加速器会一直卡在拉基础镜像上。
按第 2 节「拉取镜像（境内必做）」写 `/etc/docker/daemon.json`：

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "registry-mirrors": ["https://docker.m.daocloud.io"]
}
JSON
sudo systemctl restart docker
docker info | grep -A2 'Registry Mirrors'    # 确认已生效
```

镜像**内部**的依赖下载已经走国内源（`registry.npmmirror.com`、清华 PyPI），不用另外配。

### 15.3 把代码和镜像弄到新机器上（二选一）

**先说清楚一件事：新机器上不需要自己装 opencode，也不需要 node / npm / python。**
opencode 由 `opencode/Dockerfile` 在构建期从 npm 装进镜像，版本钉在
`ARG OPENCODE_VERSION=1.18.31`（升级就改这一行再重建）。宿主机上需要的只有
`git` 和 `docker`（含 compose v2），别的一律不用装。

构建期要下载两处依赖，所以新机器得能访问这两个镜像源（都在境内，一般没问题）：

| 镜像 | 下载源 |
|---|---|
| `agent-opencode` | `registry.npmmirror.com`（npm 装 opencode-ai） |
| `agent-gateway` | 清华 PyPI |

**方式 A：新机器能上外网** —— 直接克隆（构建见 15.6）：

```bash
git clone https://ghfast.top/https://github.com/zhliu577-a11y/QinAN.git
cd QinAN/deploy
```

直连 GitHub 不通时用上面的加速前缀（同一个仓库，另有 `ghproxy.net` 可用）。

**方式 B：新机器不能上外网 / 网络很差** —— 在任意一台能上网的机器上构建好，
把镜像打包带过去（已实测，包体约 **320 MB**）：

```bash
# 在能上网的机器上：先克隆仓库并完整构建过一次，再打包镜像
git clone https://ghfast.top/https://github.com/zhliu577-a11y/QinAN.git
cd QinAN/deploy && docker compose up -d --build
docker save agent-opencode:local agent-gateway:local agent-mock-model:local \
            nginx:1.27-alpine -o qinan-images.tar

# 把整个 QinAN 目录和 qinan-images.tar 一起拷到新机器（U 盘 / scp 都行）

# 新机器上：装好 docker，然后
cd QinAN/deploy
docker load -i /home/user/qinan-images.tar   # 期望 4 行 Loaded image
docker compose up -d                         # 注意：不带 --build
```

方式 B 里新机器**不执行任何 `git clone`**（它上不了外网），代码是从那台能上网的
机器上拷过去的。几点都实测过：

- **带什么过去**：四个镜像共 361 MB（save 成单个 tar 是 321 MB），
  加上代码（整个仓库 840 KB / 58 个文件）。一个 U 盘装得下。
- **tar 不用再 gzip**：Docker 的层本身就是压缩的，实测 321 MB → gzip 后 319 MB，
  白费一次压缩时间。
- **只拷 `deploy/` 目录也够**。`build:` 的 context 目录不存在并不影响启动——
  我在缺 context 的情况下实跑了一次 `docker compose up -d`，compose 发现镜像
  已存在就直接用了，根本不去碰那个目录。但还是建议把整个仓库拷过去：多 3 MB，
  换来以后想改配置、想重新构建、想 `git pull` 时源码都在。
- **别再用 `up -d --build`**：那会重新走一遍构建、又需要联网，而且这时 context
  若不存在就会失败。改配置用 `--force-recreate`（15.7），改代码才需要回到
  能上网的机器重建并重新 save/load。
- 顺带一提，万一某个镜像没 load 成功，失败模式很干脆：compose 会转而尝试构建，
  然后报 context 找不到。见到那个报错就回头查 `docker load` 少拿了哪个镜像。

### 15.4 写 `.env`（唯一需要手工填的文件）

```bash
cp .env.example .env
chmod 600 .env        # 里面有 JWT 与实例密码
```

**必改项**（其余保持默认）：

| 变量 | 填什么 | 生成 |
|---|---|---|
| `PUBLIC_BASE_URL` | `https://你的域名` | —— |
| `JWT_SECRET` | 随机 | `openssl rand -hex 32` |
| `ADMIN_TOKEN` | 随机 | `openssl rand -hex 24` |
| `OC1_PASSWORD` / `OC2_PASSWORD` / `OC3_PASSWORD` | 三把**互不相同**的随机值 | 各 `openssl rand -hex 24` |
| `CALLBACK_HMAC_SECRET` | 随机；完全不用回调可以不管 | `openssl rand -hex 32` |
| `CALLBACK_ALLOWED_HOSTS` | App 后端的域名，逗号分隔；回调地址**只允许 https**，这里空着会拒掉所有回调 | —— |
| `EXCHANGE_HMAC_SECRET` | 只有用模式 B 才填，与 App 团队约定同一个值 | `openssl rand -hex 32` |
| `MODEL_*` | 见 15.7 | —— |

再确认三项默认值符合预期：

- `MOCK_MODE=false` —— 保持 false。**上线前最后一个动作就是确认它是 false、且 `MODEL_BASE_URL` 不是 `mock-model`。**
- `DEFAULT_DAILY_QUOTA` —— 每人每天的任务条数，默认 30。
- `CORS_ALLOW_ORIGINS` —— 原生 App 留空；H5 / WebView 填来源域名。

`.env` 整份被 `env_file` 注入 gateway 容器，改完必须重建容器才生效（`restart` 不重新读）。
注意 `.env` 里的变量是**分给两边**的：配额/限流/回调/`JWT` 等只影响 gateway，
`MODEL_BASE_URL`/`MODEL_API_KEY` 只影响 opencode，而 `MODEL_PROVIDER`/`MODEL_NAME`
两边都读。重建规则见第 5 节「换模型 / 换服务商」。

### 15.5 放证书

```bash
mkdir -p certs
# 把正式证书放成这两个文件名（nginx.conf 里写死的）：
#   certs/fullchain.pem
#   certs/privkey.pem
```

没有域名时只能用自签证书演练（`./gen-self-signed-cert.sh <IP或域名>`，见第 2 节）。
自签会让 App 报证书不受信任，**不要用于生产**。

### 15.6 首次构建与启动

```bash
docker compose config >/dev/null && echo "语法 OK"
docker compose up -d --build        # 首次要构建三个镜像，慢是正常的
docker compose ps                   # 六个容器都应是 healthy
```

六个容器：`nginx`（唯一对外）、`gateway`、`opencode-1/2/3`、`mock-model`
（模拟模型，接上真实模型后可以 `docker compose stop mock-model` 省资源）。

```bash
curl -s https://你的域名/healthz            # 期望 ok
curl -s https://你的域名/api/v1/health      # 期望 engine: ready
docker compose logs --tail 50 gateway
```

注意 `/api/v1/health` 的 `engine: ready` 只说明三个 opencode 实例在，**不代表模型能用**。

### 15.7 接真实模型

默认值指向内置模拟模型，换成真实服务商只改四行：

```
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-chat
MODEL_BASE_URL=https://api.deepseek.com/v1
MODEL_API_KEY=sk-真实密钥
```

- `MODEL_NAME` 必须是**该地址真实提供的 id**：写错启动时不报错，第一次调用才失败。
  用 `curl -s -H "Authorization: Bearer $MODEL_API_KEY" "$MODEL_BASE_URL/models"` 查实际有哪些。
- `MODEL_BASE_URL` 必须 OpenAI 兼容（`GET /models` 列模型、`POST /chat/completions` 对话）。
- 改完 `.env` 用**不点名**的 `up -d` 落地，**不用重新构建镜像**：

```bash
docker compose up -d        # 不要点名服务，也不要 --force-recreate
docker compose ps           # 等六个容器都 healthy 再提交任务
```

  为什么强调「不点名」：`up -d` 后面点名了服务，compose 就只处理那几个，没点名的
  即使配置变了也不动——`MODEL_PROVIDER`/`MODEL_NAME` gateway 自己也读一份，
  漏掉 gateway 会导致「换了模型但仍按旧 model id 发请求」，详见第 5 节实测记录。

- 换**服务商**（改 `MODEL_PROVIDER` 的取值）还要动 `opencode/config/opencode.json`
  里覆写的那个 provider 键名（当前是 `deepseek`），两处必须同名，然后
  `docker compose up -d --build opencode-1`。
- 想确认运行时到底在用哪个模型，不用猜，看网关自己报的：

```bash
ADMIN_TOKEN=$(grep ^ADMIN_TOKEN= .env | cut -d= -f2-)
docker compose exec gateway curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://127.0.0.1:8080/internal/status | python3 -m json.tool | grep -A3 '"config"'
```

`config.model_provider` / `config.model_name` 就是网关**此刻**会下发的那两个值。
它和 `.env` 不一致，就说明网关没重建。

连通性自测按第 5 节的两层做法：先从实例内直接 curl 模型端点（验证地址 + 密钥 +
模型 id），再走完整链路提交一个真实任务。

然后跑一个真实任务，并**按第 5 节「模拟覆盖不到的东西」那张表逐项确认**：
tokens/cost、超时回收、抓取失败、多轮工具调用在模拟下都证明不了。

### 15.8 开户

管理接口 `/internal/*` 在 nginx 上是 `deny all`，必须从容器内打（第 6 节）：

```bash
ADMIN_TOKEN=$(grep ^ADMIN_TOKEN= .env | cut -d= -f2-)
docker compose exec gateway curl -s -X POST http://127.0.0.1:8080/internal/users \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"强密码","display_name":"张三","daily_quota":30}'
```

模式 B（App 侧可信直传）由 `/api/v1/auth/exchange` 自动开户，不用手工开。

> **目前没有「用户自助改密码」的接口**。改密码只能走管理接口：
> `PATCH /internal/users/{id}`，body `{"password":"新密码"}`。所以密码要在开户后
> 立刻设成最终值再交给本人，重置也由运营方执行。要终端用户能自己改，需要 App 团队
> 与网关一起加一个接口。

### 15.9 上线前检查表

逐条打勾，每条都是能验的：

- [ ] `docker compose ps` 六个容器（含 `mock-model`）都是 `healthy`
- [ ] `https://域名/healthz` 返回 `ok`
- [ ] `https://域名/api/v1/health` 返回 `engine: ready`，且 `pool_total` 等于实例数
- [ ] `./ops.sh instances` 里每个实例都是 `idle`（或 `busy`），没有 `unhealthy`
- [ ] `MOCK_MODE=false` **且** `MODEL_BASE_URL` 不是 `mock-model`
- [ ] 真实模型上提交过一个 url 任务，`source.fetched_chars` 是真实数字
- [ ] `.env` 里默认密码一个不剩：`grep -n 'please-change-me' .env` 没有输出
- [ ] `.env` 权限是 `600`，且没被提交进 git（`git status` 里看不到它）
- [ ] 安全组只开了 80/443；`docker compose ps` 里 gateway / opencode 没有端口映射
- [ ] 宿主机重启后能自愈（第 14 节：容器 `restart: unless-stopped` + `docker.service` enabled）
- [ ] `./ops.sh backup` 跑通一次，`backup/` 下生成了两个 tgz

### 15.10 上线之后

- 日常运维只有一个入口：`./ops.sh status`（第 14 节）
- 扩容：第 8 节（四处同步改，用 `./ops.sh instances` 验证）
- 证书续期：第 9 节；备份与恢复：第 10 节
- 定期 `./ops.sh backup`，并把 `backup/` 复制到**另一台机器**——
  备份和业务数据放在同一台机器上不算备份

## 16. 改动速查：改什么用什么命令

**先记住两条前提**：

1. 环境变量只在**容器创建时**注入，`docker compose restart` 读不到新值。
   而「进程自己崩了」这种情况 `restart: unless-stopped` 已经处理了，不用手工干预。
2. **改 `.env` 不要点名服务、不要加 `--force-recreate`**，直接 `docker compose up -d`。
   compose 会把 `env_file` 内容算进每个服务的配置哈希，自己算出该重建谁；
   一旦点名，没点到的服务即使配置变了也不会动（第 5 节实测过这个坑）。

所有命令都在 `deploy/` 目录下执行。

| 你要改的东西 | 命令 | 备注 |
|---|---|---|
| **任何 `.env` 改动**（配额、模型、密码、密钥…） | 改 `.env` → `./ops.sh apply`（等价于不点名的 `docker compose up -d`） | 一律同一条命令，不用记「谁该重建」 |
| nginx 配置（限流、超时、路由） | 改 `nginx.conf` → `docker compose up -d --force-recreate nginx` | 单文件 bind mount，内容变化 compose **检测不到**，必须强制重建 |
| agent 权限、`agents/*.md`、`opencode.json` | 改文件 → `docker compose up -d --build opencode-1` | 这些**构建进镜像**，`--force-recreate` 不够 |
| opencode 版本 | 改 `opencode/Dockerfile` 的 `ARG OPENCODE_VERSION` → `docker compose up -d --build opencode-1` | 同上 |
| gateway 代码 | 改 `gateway/app/**` → `docker compose up -d --build gateway` | |
| 实例数量 | 改 `docker-compose.yml` 四处（第 8 节）→ `docker compose up -d` | 不用 `--build`，新实例复用现成镜像 |
| 证书 | 覆盖 `certs/*.pem` → `docker compose exec nginx nginx -s reload` | 证书是 bind mount，`reload` 就够（与 `nginx.conf` 不同） |
| 就是想无条件重启某几个服务 | `./ops.sh restart gateway`／`restart opencode`／`restart all` | 点名 + `--force-recreate`，**不用于 `.env` 落地** |

一句话版本：**`.env` 改动 → `./ops.sh apply`；镜像里的东西（代码、`opencode.json`、
agent 定义、opencode 版本）→ `--build`；`nginx.conf` → `--force-recreate nginx`；
证书 → `reload`。**

**常用开关与查看**：

```bash
./ops.sh status                       # 先看这个：容器 + 对外健康 + 网关自检
./ops.sh apply                        # 把 .env 的改动落地（compose 自己算该重建谁）
./ops.sh instances                    # 每个实例忙不忙、失败几次、挂了几个会话
./ops.sh logs gateway -f              # 跟日志；ops.sh logs <服务名> 可换 nginx/opencode-1
./ops.sh user zhangsan                # 单用户：配额用量、任务分布、实例绑定
./ops.sh heal                         # 只重建不健康的容器
./ops.sh backup                       # 备份两个数据卷

docker compose ps                     # 容器状态
docker compose stop opencode-3        # 临时下线一个实例（约 30s 后网关标记不可用并停止派活）
docker compose start opencode-3       # 恢复（实测约 5s 内回到 idle）
./ops.sh shell gateway                # 进容器
```

**改完一定要验**（这三条覆盖了绝大多数「改完没生效」的情况）：

```bash
docker compose ps                      # 1) 六个容器都 healthy 再继续
curl -s https://你的域名/api/v1/health  # 2) engine: ready，pool_total 等于实例数
ADMIN_TOKEN=$(grep ^ADMIN_TOKEN= .env | cut -d= -f2-)
docker compose exec gateway curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://127.0.0.1:8080/internal/status | python3 -m json.tool   # 3) config 段 = 网关当前生效的配置
```

重建实例后**前 10~30 秒别提交任务**：池子的健康检查每 10 秒一轮、连续 3 次失败才
标记不可用，这段时间网关仍会往还没 bootstrap 完的实例派活，实测会让任务直接 `failed`。
