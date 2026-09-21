# 智能体摘要服务 API 契约 v1

本文档面向 App 开发方。本服务只提供 HTTP API，不含界面。

## 1. 基本约定

- Base URL：`https://{host}/api/v1`
- 编码：UTF-8；请求与响应均为 `application/json`，流式接口除外
- 鉴权：`Authorization: Bearer <access_token>`
- 时间：ISO 8601 带时区，例如 `2026-09-21T14:03:11+08:00`
- 幂等：`POST /tasks` 必须带 `Idempotency-Key`（建议 UUID），24 小时内同 Key 返回同一任务
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

### 2.3 刷新与登出

- `POST /auth/refresh`，body `{ "refresh_token": "rt_..." }` → 返回新的 `access_token`
- `POST /auth/logout` → `204`，使当前 refresh_token 失效

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
| `instruction` | string | 否 | 自定义要求，缺省为"提炼要点并输出 Markdown" |
| `max_output_chars` | int | 否 | 输出上限，默认 1200，最大 4000 |
| `callback_url` | string | 否 | 终态回调地址，必须 HTTPS 且在服务端白名单内 |
| `client_task_id` | string | 否 | 你们侧的任务 ID，原样回传，便于对账 |

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
- 同一用户同时只允许 1 个任务处于 `queued`/`running` 状态，超出发 `429 QUEUE_FULL`
- 每日配额用尽发 `429 QUOTA_EXCEEDED`

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
  "items": [ { "task_id": "tsk_9f3a1c", "status": "succeeded", "created_at": "...", "summary_head": "前 100 字预览" } ],
  "next_cursor": "eyJvZmZzZXQiOjIwfQ=="
}
```

- `limit` 默认 20，最大 50；`next_cursor` 为 `null` 表示没有更多
- **只返回当前用户自己的任务**

### 5.3 取消

`POST /tasks/{task_id}/cancel` → `200 { "task_id": "...", "status": "canceled" }`

仅对 `queued`/`running`/`streaming` 有效；已终态返回 `409 TASK_NOT_CANCELABLE`。

## 6. 流式获取结果

### 6.1 SSE

`GET /tasks/{task_id}/events`，`Accept: text/event-stream`

```
event: status
data: {"status":"running"}

event: delta
data: {"text":"# 摘要\n"}

event: delta
data: {"text":"- 要点一\n"}

event: done
data: {"status":"succeeded","usage":{"tokens_out":812}}
```

### 6.2 WebSocket

`WS /ws/tasks/{task_id}?token=<access_token>`

消息体与 SSE 的 `data` 一致，外层带类型：

```json
{ "type": "delta", "text": "# 摘要\n" }
```

- WebSocket 需带 `token` 查询参数（部分客户端无法设置 Header）
- 服务端每 20 秒发 `{ "type": "ping" }`，客户端应回 `{ "type": "pong" }`

### 6.3 重连补发

- SSE 支持 `Last-Event-ID`，WebSocket 支持 `{ "type": "attach", "last_seq": 128 }`
- 补发完成后转入实时推送；断线期间产生的增量片段不会丢失

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
  "finished_at": "2026-09-21T14:04:20+08:00"
}
```

- 请用原始请求体校验签名，并返回 `2xx`
- 非 `2xx` 或超时（5s）将重试 3 次（间隔 5s / 30s / 120s）
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
| 400 | `INVALID_INPUT` | 参数不合法（含 URL 格式、文本超长） |
| 401 | `UNAUTHORIZED` | Token 缺失/过期/吊销 |
| 403 | `FORBIDDEN` | 无权访问该任务（非本人） |
| 404 | `NOT_FOUND` | 任务不存在或已过 30 天留存期 |
| 409 | `TASK_NOT_CANCELABLE` | 任务已终态，无法取消 |
| 429 | `QUOTA_EXCEEDED` | 当日配额用尽 |
| 429 | `QUEUE_FULL` | 个人队列已满（正在处理 ≥ 1 个） |
| 429 | `RATE_LIMITED` | 请求过于频繁，看 `Retry-After` |
| 502 | `UPSTREAM_ERROR` | 智能体后端异常，可重试 |
| 504 | `TIMEOUT` | 执行超时 |

任务级的错误码（出现在 `GET /tasks/{id}` 的 `error.code` 里，不是 HTTP 状态码）：

| code | 含义 |
|---|---|
| `UPSTREAM_ERROR` | 智能体实例异常，自动重试后仍失败 |
| `TIMEOUT` | 超过 `TASK_TIMEOUT_SECONDS` |
| `GATEWAY_RESTARTED` | 网关重启打断了任务，自动重试后仍未完成，需要重新提交。这是运维动作导致的，不是用户输入的问题 |

429 有两种来源，App 要分开处理：

- **网关返回的 429**（`QUOTA_EXCEEDED` / `QUEUE_FULL` / `RATE_LIMITED`）：标准 JSON 错误体，
  带 `Retry-After`。按 `code` 给用户不同文案即可。
- **nginx 返回的 429**（请求频率超过 `limit_req`）：body 是 nginx 的 HTML 错误页，
  **没有** `Retry-After`。App 见到非 JSON 的 429 应按固定间隔退避重试，不要当业务错误展示。

不提供 `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`（未实现）。

## 9. 健康检查

`GET /health`（无需鉴权）

```json
{ "status": "ok", "engine": "ready", "pool_idle": 2, "pool_total": 3, "queue_len": 1 }
```

`engine` 为 `ready` / `degraded` / `down`，可用于 App 侧降级提示。

## 10. 联调环境

- 沙箱：`https://{host}/api/v1`，开启 `MOCK_MODE` 后所有状态流转、错误码与生产一致，延迟约 3~8 秒，`result_md` 为固定样例文本
- 沙箱账号与正式账号隔离，配额独立
- 可用 `GET /openapi.json` 生成各语言 SDK

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
