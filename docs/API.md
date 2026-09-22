# 智能体摘要服务 API 契约 v1

本文档面向 App 开发方。本服务只提供 HTTP API，不含界面。

## 1. 基本约定

- Base URL：`https://{host}/api/v1`
- 编码：UTF-8；请求与响应均为 `application/json`，流式接口除外
- 鉴权：`Authorization: Bearer <access_token>`
- 时间：ISO 8601 带时区，例如 `2026-09-21T14:03:11+08:00`
- 幂等：`POST /tasks` 应带 `Idempotency-Key`（建议 UUID）。同一用户用同一个 Key 重复提交，
  服务端直接返回**第一次建的那个任务**（不设时间窗，也不看它当前是什么状态）；
  不带这个头则不去重，会重复建任务
- 追踪：响应头 `X-Request-Id`，报障时请提供
- 版本：路径带 `/v1`，破坏性变更会升 `/v2`，`/v1` 至少并行维护 6 个月

## 2. 鉴权

### 2.1 模式 A：账号密码换 Token

`POST /auth/token`

```json
{ "username": "zhangsan", "password": "******", "device_id": "ios-9f2c1a" }
```

`200`

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "Bearer",
  "expires_in": 43200,
  "refresh_token": "rt_7c1f...",
  "user": { "id": 12, "username": "zhangsan", "display_name": "张三", "daily_quota": 30 }
}
```

`access_token` 有效期 12 小时，`refresh_token` 30 天。

字段上限：`username` ≤ 64、`password` ≤ 256、`device_id` ≤ 128 字符，超限返回 `400 INVALID_INPUT`。

### 2.2 模式 B：App 侧可信直传（推荐给已有用户体系的 App）

App 后端用双方约定的共享密钥签名，无需用户二次登录。

`POST /auth/exchange`

```json
{
  "external_user_id": "u_88231",
  "display_name": "张三",
  "timestamp": 1789000000,
  "signature": "hex(hmac_sha256(secret, external_user_id + \".\" + timestamp))"
}
```

- `timestamp` 为 Unix 秒，与服务端时差超过 **300 秒**直接拒绝，防重放
- 首次调用会自动开户（`daily_quota` 取默认值），用户信息随后可人工调整
- 响应与 2.1 相同，但返回的 `user.id` 为服务端内部 ID，请以它为准
- 字段上限：`external_user_id` ≤ 128、`display_name` ≤ 64 字符，`signature` 为 16~128 字符

签名算法（`signature` 用十六进制小写，服务端比较前会转小写，所以大写也接受）：

```python
import hashlib, hmac, time

secret = "与网关约定的共享密钥"          # 对应网关 deploy/.env 的 EXCHANGE_HMAC_SECRET
external_user_id = "u_88231"
ts = int(time.time())
message = f"{external_user_id}.{ts}"
signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
```

必须知道的几点：

- **密钥要双方一致，且必须在网关侧配置。** 网关的 `EXCHANGE_HMAC_SECRET` 为空时会直接返回
  `403 FORBIDDEN`「未启用 App 侧可信直传」——这不是签名算错了，是模式 B 没开。
  生成密钥：`openssl rand -hex 32`，填进 `deploy/.env` 后 `./ops.sh restart gateway`。
- 密钥只放服务端之间，**不要下发到 App 客户端里**。签名必须由 App 后端计算。
- `external_user_id` 一旦开户就与用户绑定，之后改名会被当成新用户（会开出新账号）。
- 换 Token 会拿到一个新的限流桶（见 `deploy/README.md` 限流一节）。建议每个终端各自换取 Token，
  不要用同一个 service token 代所有用户转发，否则限流会退化成全局桶。
- **每次 `/auth/exchange` 都会新建一个 api_client（一个新的登录会话）**，同时刷新限流桶。
  App 侧必须「换一次、用 12 小时」，不要每个请求都调 exchange，否则会不断堆积
  refresh token 并使限流失效。
- 同一签名在 300 秒容差窗口内可重复使用（服务端不做 nonce 去重）。

### 2.3 刷新与登出

- `POST /auth/refresh`，body `{ "refresh_token": "rt_..." }` → 返回新的 `access_token`。
  只换发 `access_token`，`refresh_token` **不轮换**，一直有效到被 logout 吊销或账号被停用
- `POST /auth/logout` → `204`，吊销当前登录会话（该会话的 `access_token` 与 `refresh_token`
  同时失效）。要带 `Authorization: Bearer <access_token>`，不需要 body

## 3. 当前用户

`GET /me`

```json
{
  "id": 12,
  "username": "zhangsan",
  "display_name": "张三",
  "daily_quota": 30,
  "used_today": 7,
  "remaining_today": 23,
  "queue_depth": 0,
  "in_flight_limit": 1
}
```

## 4. 提交任务

`POST /tasks`

请求头：`Idempotency-Key: 7b1c...`

```json
{
  "kind": "url",
  "url": "https://example.com/article",
  "instruction": "总结要点，输出 Markdown，控制在 800 字内",
  "max_output_chars": 1200,
  "callback_url": "https://app.example.com/hooks/agent",
  "client_task_id": "a1b2c3"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `kind` | string | 是 | `url` 或 `text` |
| `url` | string | `kind=url` 必填 | 必须 `http(s)://`，长度 ≤ 2048 |
| `text` | string | `kind=text` 必填 | 长度 ≤ 200000 字符 |
| `instruction` | string | 否 | 自定义要求，缺省为"提炼要点并输出 Markdown"，长度 ≤ 4000 |
| `max_output_chars` | int | 否 | 输出上限，取值 100~4000，默认 1200 |
| `callback_url` | string | 否 | 终态回调地址，必须 HTTPS 且在服务端白名单内 |
| `client_task_id` | string | 否 | 你们侧的任务 ID，原样回传，便于对账，长度 ≤ 64 |

`201`

```json
{
  "task_id": "tsk_9f3a1c",
  "status": "queued",
  "queue_pos": 2,
  "estimated_wait_seconds": 45,
  "created_at": "2026-09-21T14:03:11+08:00"
}
```

- `queue_pos` 从 1 开始；已开始执行时为 `0`
- 每日配额用尽发 `429 QUOTA_EXCEEDED`
- 两个**独立**的闸门，各自超限都返回 `429 QUEUE_FULL`，区别在 `error.message` 和 `retry_after`：
  - **在飞闸门**：`running` + `streaming` 的任务达到 `MAX_IN_FLIGHT_PER_USER`（默认 1）→
    "你当前已有 N 个任务在处理中"，`retry_after: 30`
  - **排队闸门**：`queued` 的任务达到 `MAX_QUEUE_DEPTH_PER_USER`（默认 3）→
    "你的待处理队列已满"，`retry_after: 60`

  也就是说默认配置下最多「1 个在飞 + 3 个排队」，第 5 个才会被拒。

## 5. 查询任务

`GET /tasks/{task_id}`

```json
{
  "task_id": "tsk_9f3a1c",
  "client_task_id": "a1b2c3",
  "kind": "url",
  "status": "succeeded",
  "queue_pos": 0,
  "result_md": "# 摘要\n\n- 要点一\n- 要点二",
  "source": { "url": "https://example.com/article", "title": "示例文章", "fetched_chars": 8213 },
  "usage": { "tokens_in": 9231, "tokens_out": 812, "duration_ms": 23800 },
  "created_at": "2026-09-21T14:03:11+08:00",
  "started_at": "2026-09-21T14:03:56+08:00",
  "finished_at": "2026-09-21T14:04:20+08:00"
}
```

`source` 字段：

| 字段 | 含义 |
|---|---|
| `url` | 提交时的目标地址；`kind=text` 时为 `null` |
| `title` | 抓到的页面标题；抓取失败、或上游只回报 URL 本身时为 `null` |
| `fetched_chars` | 实际拿到的正文字符数（详见下表） |

`fetched_chars` 的取值约定：

| 取值 | 含义 |
|---|---|
| 正整数 | 抓取成功，数值为正文长度 |
| `0` | 抓取工具执行过，但正文为空 |
| `null` | 一次都没抓成（站点拒绝、超时、需要脚本渲染等），结果由模型自行说明 |

对 `kind=text` 的任务，该字段是提交文本的长度。

### 5.1 任务状态

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `queued` | 排队中 | 否 |
| `running` | 执行中（已开始，尚无输出） | 否 |
| `streaming` | 正在产出内容 | 否 |
| `succeeded` | 成功 | 是 |
| `failed` | 失败，见 `error` | 是 |
| `canceled` | 已取消 | 是 |
| `timeout` | 超时（默认 180s） | 是 |

失败时返回：

```json
{
  "task_id": "tsk_9f3a1c",
  "status": "failed",
  "error": { "code": "UPSTREAM_ERROR", "message": "智能体后端异常" }
}
```

**抓取失败不是任务失败**：目标站点拒绝、超时、需要脚本渲染时，webfetch 的结果
会作为普通工具结果交回模型，任务通常会正常 `succeeded`，只是 `source.fetched_chars`
为 `null`（见 5 节），由模型在 `result_md` 里说明抓不到。因此没有单独的
`FETCH_BLOCKED` 错误码——调用方判断依据是 `fetched_chars`，不是 `error`。

### 5.2 历史列表

`GET /tasks?limit=20&cursor=eyJ...&status=succeeded`

```json
{
  "items": [
    {
      "task_id": "tsk_9f3a1c",
      "status": "succeeded",
      "kind": "url",
      "created_at": "2026-09-21T14:03:11+08:00",
      "summary_head": "前 100 字预览"
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjIwfQ=="
}
```

- `limit` 默认 20，最大 50；`next_cursor` 为 `null` 表示没有更多，否则原样传回 `cursor` 取下一页
- `status` 可选，取值见 5.1 的状态表（如 `succeeded`）；不传则返回全部状态
- `summary_head` 是 `result_md` 的前 100 字，没有结果时为 `null`
- 留存期口径与 `GET /tasks/{id}` 一致：过了留存期的终态任务在两边都看不到
- **只返回当前用户自己的任务**

### 5.3 取消

`POST /tasks/{task_id}/cancel` → `200 { "task_id": "...", "status": "canceled" }`

仅对 `queued`/`running`/`streaming` 有效；已终态返回 `409 TASK_NOT_CANCELABLE`。

## 6. 流式获取结果

### 6.1 SSE

`GET /tasks/{task_id}/events`，`Accept: text/event-stream`

鉴权同其他接口（`Authorization: Bearer <access_token>`）；浏览器 `EventSource` 这类
不能自定义 Header 的客户端，可以改用 `?token=<access_token>`（SSE 与 WebSocket 都支持）。

```
id: 1
event: status
data: {"type":"status","status":"running"}

id: 2
event: delta
data: {"type":"delta","text":"# 摘要\n"}

id: 3
event: delta
data: {"type":"delta","text":"- 要点一\n"}

id: 4
event: done
data: {"type":"done","status":"succeeded"}
```

事件类型只有四种：`status`、`delta`、`done`、`error`。

- `id` 是事件序号（`seq`），与 `Last-Event-ID` 配合用于断线补发（见 6.3）
- `data` 里同时带 `type` 字段，取值与 `event:` 行相同。`event:` 行是给浏览器 `EventSource`
  用的；自研客户端直接读 `data.type` 即可
- `done` 只带 `status`，**不带用量**；`usage` 请用 `GET /tasks/{id}` 取
- `error` 带 `{"status":"failed","error":{"code":"...","message":"..."}}`
- 没有事件推送时，服务端发**注释帧** `: ping`（每 `SSE_PING_INTERVAL_SECONDS` 秒一次，默认 15s）。
  它没有 `event:` / `data:`，按 SSE 规范应被忽略
- 收到 `done` 或 `error` 后服务端会关闭连接，客户端不必主动断开

### 6.2 WebSocket

`WS /ws/tasks/{task_id}?token=<access_token>`

消息体与 SSE 的 `data` 一致，外层带类型：

```json
{ "type": "delta", "text": "# 摘要\n" }
```

- WebSocket 需带 `token` 查询参数（部分客户端无法设置 Header）
- 连接建立后**总是先按 `seq` 重放该任务的完整事件流**，再转入实时推送
- 服务端每 15 秒（与 SSE 心跳同一个值 `SSE_PING_INTERVAL_SECONDS`）发 `{ "type": "ping" }`，
  客户端可以回 `{ "type": "pong" }`（服务端不校验，也不依赖它保活）

### 6.3 重连补发

- **SSE**：重连时带上 `Last-Event-ID: <最后收到的 id>`，或用 `?last_seq=<n>` 查询参数，
  服务端只补发 `seq > n` 的事件；补发完成后转入实时推送，断线期间产生的增量片段不会丢失
- **WebSocket**：每次连接都**从 seq 0 重放完整历史**，然后转实时推送。客户端不需要、也不要发
  attach 帧（发了会被忽略）。因此重连后**必须按 `seq` 去重**，已渲染过的增量不要再拼一遍

  想「只补增量」就用 SSE 的 `Last-Event-ID`；WebSocket 的取舍是简单，代价是重连会重收全量。

### 6.4 兜底建议

移动网络下长连接不可靠，**请同时实现 `GET /tasks/{id}` 轮询**（建议 2s → 5s 退避）。流式接口连不上不应导致功能不可用。

## 7. 完成回调

任务进入终态时，服务端向 `callback_url` 发起：

```
POST {callback_url}
X-Agent-Signature: sha256=<hex(hmac_sha256(callback_secret, raw_body))>
Content-Type: application/json
```

```json
{
  "task_id": "tsk_9f3a1c",
  "client_task_id": "a1b2c3",
  "status": "succeeded",
  "result_md": "# 摘要\n...",
  "error": null,
  "finished_at": "2026-09-21T14:04:20+08:00"
}
```

- 请用原始请求体校验签名，并返回 `2xx`
- 非 `2xx` 或超时（默认 5s）会重试，**总共尝试 `CALLBACK_MAX_ATTEMPTS`（默认 3）次**，
  间隔取自 `CALLBACK_RETRY_DELAYS`（默认 5s / 30s / 120s）
- 任务失败时 `error` 是 `{"code":"...","message":"..."}`，成功时为 `null`
- 回调失败不影响任务本身状态，可随时用 `GET /tasks/{id}` 补拉

## 8. 错误格式

所有非 `2xx` 响应统一为：

```json
{
  "error": {
    "code": "QUEUE_FULL",
    "message": "你当前已有 1 个任务在处理中，请等待完成或取消",
    "retry_after": 30
  }
}
```

| HTTP | code | 含义 |
|---|---|---|
| 400 | `INVALID_INPUT` | 参数不合法（含 URL 格式、文本/字段超长、cursor 不合法） |
| 401 | `UNAUTHORIZED` | Token 缺失/过期/已吊销/账号被禁用；**登录失败（用户名或密码错误）也是 401** |
| 403 | `FORBIDDEN` | 无权访问该任务（非本人）；或模式 B 未启用时的 `/auth/exchange` |
| 404 | `NOT_FOUND` | 任务不存在或已过 30 天留存期 |
| 409 | `TASK_NOT_CANCELABLE` | 任务已终态，无法取消 |
| 429 | `QUOTA_EXCEEDED` | 当日配额用尽 |
| 429 | `QUEUE_FULL` | 在飞任务已达上限（默认 1）或个人队列已满（默认 3），看 `error.message` 区分 |
| 502 | `UPSTREAM_ERROR` | 智能体后端异常，可重试 |
| 504 | `TIMEOUT` | 执行超时 |

任务级的错误码（出现在 `GET /tasks/{id}` 的 `error.code` 里，不是 HTTP 状态码）：

| code | 含义 |
|---|---|
| `UPSTREAM_ERROR` | 智能体实例异常，自动重试后仍失败 |
| `TIMEOUT` | 超过 `TASK_TIMEOUT_SECONDS` |
| `GATEWAY_RESTARTED` | 网关重启打断了任务，自动重试后仍未完成，需要重新提交。这是运维动作导致的，不是用户输入的问题 |

429 有两种来源，App 要分开处理：

- **网关返回的 429**（只有 `QUOTA_EXCEEDED` 和 `QUEUE_FULL`）：标准 JSON 错误体，
  带 `Retry-After`。按 `code` 给用户不同文案即可。
  网关侧**没有**限流逻辑，也没有 `RATE_LIMITED` 这个 code，别照着写分支。
- **nginx 返回的 429**（请求频率超过 `limit_req`，例如同一 token 每分钟超过 120 次）：
  body 是 nginx 的 HTML 错误页，
  **没有** `Retry-After`。App 见到非 JSON 的 429 应按固定间隔退避重试，不要当业务错误展示。

不提供 `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`（未实现）。

## 9. 健康检查

`GET /health`（无需鉴权）

```json
{ "status": "ok", "engine": "ready", "pool_idle": 2, "pool_total": 3, "queue_len": 1 }
```

`engine` 为 `ready` / `degraded` / `down`，可用于 App 侧降级提示。

- `ready`：至少有一个空闲实例；`degraded`：实例都活着但都在忙；`down`：没有可用实例
- `status` 为 `ok` / `degraded`，后者表示网关自己连不上数据库（此时不要提交任务）
- 这个接口不查模型是否可用：`engine: ready` 时模型仍可能是坏的

## 10. 联调环境

- 沙箱：`https://{host}/api/v1`，开启 `MOCK_MODE` 后所有状态流转、错误码与生产一致，延迟约 3~8 秒，`result_md` 为固定样例文本
- 沙箱账号与正式账号隔离，配额独立
- 可用 `GET /openapi.json` 生成各语言 SDK。线上只放行这一个路径；
  FastAPI 自带的 `/docs`、`/redoc` 没有对外暴露（本地起服务时才看得到）

## 11. 联调清单

1. 用沙箱账号走通 `/auth/token` → `/tasks` → `/events` → `GET /tasks/{id}` 全链路
2. 验证 `Idempotency-Key` 重发不产生重复任务
3. 验证断网重连后能补齐增量片段
4. 处理 429（`QUEUE_FULL` / `QUOTA_EXCEEDED`）的界面提示；
   抓取不成功时看 `source.fetched_chars === null`，而不是等一个错误码
5. 实现 `callback_url` 并校验 `X-Agent-Signature`
6. 确认长文本（接近 20 万字符）与超长 URL 的客户端校验

## 12. 待确认事项

- 是否采用模式 B（App 侧可信直传），若采用需交换共享密钥与回调密钥
- 默认每日配额与个人同时任务数（当前默认 30 次/日、1 个并发）
- 任务结果留存期（当前 30 天）
