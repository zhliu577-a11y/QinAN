# QinAN

境内服务器上的**移动端智能体摘要服务**：让十几二十个人通过手机 App，把「一段文本」或
「一个网址」交给智能体，拿回一份摘要。

引擎是 [opencode](https://opencode.ai)（开源终端 AI 智能体），以实例池方式部署；
本项目提供**纯 HTTP API**，手机端 App 由其他人开发。

## 架构

```
App ──HTTPS+Bearer──▶ nginx（唯一对外，80/443；TLS/限流/SSE 不缓冲）
                       │ 内网 http
                       ▼
                     gateway（FastAPI，单 worker；鉴权·任务队列·公平派发·SSE 扇出·回调）
                       │ 内网 http          └─ SQLite（卷 gateway-data）
                       ▼
                  opencode-1 / 2 / 3（实例池，各一把独立密码）
                       │ OpenAI 兼容协议
                       ▼
                  模型服务（生产：真实厂商；演练：内置 mock-model）
```

四个要点：

- **只有 nginx 有端口。** gateway 与 opencode 都没有 `ports:`，宿主机也连不上。
- **opencode 没有用户体系**（`/session` 全局可见），所以鉴权与越权防护 100% 由网关承担。
- **gateway 必须单 worker。** 实例池状态与路由表在进程内存里，多进程会互相打架。
- **智能体只会 `webfetch`。** `bash`/`read`/`write`/`edit` 等全部 deny。

## 从零开始

```bash
# 1. 装 docker（境内记得配镜像加速器，见手册 3.2）
# 2. 取代码
git clone https://ghfast.top/https://github.com/zhliu577-a11y/QinAN.git
cd QinAN/deploy

# 3. 写配置（必改：PUBLIC_BASE_URL、JWT_SECRET、ADMIN_TOKEN、三把 OC*_PASSWORD）
cp .env.example .env && chmod 600 .env

# 4. 放证书
mkdir -p certs   # certs/fullchain.pem 与 certs/privkey.pem

# 5. 起
docker compose up -d --build
docker compose ps                       # 六个容器都应是 healthy
curl -s https://你的域名/api/v1/health   # engine: ready
```

**完整步骤（含接真实模型、开户、验收清单）见 [`docs/操作手册.md`](docs/操作手册.md) 第 3 章。**

## 日常运维

先 `./ops.sh status`。改完 `.env` 用 `./ops.sh apply`——**不要点名服务、不要加
`--force-recreate`**，让 compose 自己算出该重建谁。

```bash
./ops.sh help      # 全部命令
```

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/操作手册.md`](docs/操作手册.md) | **主线**：架构 · 从零构建 · 使用 · 变更 · 排错 |
| [`deploy/README.md`](deploy/README.md) | 部署与运维的细节和踩坑记录 |
| [`docs/API.md`](docs/API.md) | 接口契约（给 App 团队） |
| [`docs/方案设计.md`](docs/方案设计.md) | 设计取舍与背景 |

## 本地开发

```bash
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r gateway\requirements.txt
$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest gateway/tests -q
```

测试用 `MOCK_MODE`，不需要 opencode、不需要模型凭据、不产生费用。
