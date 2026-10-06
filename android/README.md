# QinAN 测试端（Android）

一个极简的安卓 App：填好服务地址和内容 → 点一下按钮 → 把固定格式的 JSON 发到
`POST {baseUrl}/tasks` → 本地轮询到终态 → 显示 `result_md`。
主要给自己/同事在手机上验收网关用，不是给最终用户的产品客户端。

## 特点

- **零第三方依赖**：只用系统自带的控件 + `java.net.HttpURLConnection` + 系统内置 `org.json`。
  没有 AndroidX、没有 Material、没有 OkHttp。
- **不用 Android Studio、不用 Gradle**：`build.ps1` 直接调 `aapt2` / `javac` / `d8` / `zipalign` / `apksigner`，
  产物只有几十 KB。
- **服务地址可改**：界面上直接输入 `Base URL`，指向任意环境的网关。
- **字段可改**：`kind`、`url`、`text`、`instruction`、`max_output_chars`、`callback_url`、`client_task_id`
  都能在界面上改；还有一个「原始 JSON 模式」，可以手改整个请求体。
- **参数会记住**：所有输入存在 `SharedPreferences` 里，下次打开还在。

## 目录结构

```
android/
├── app/src/main/
│   ├── AndroidManifest.xml
│   ├── java/com/qinan/agent/
│   │   ├── ApiClient.java       # 网关客户端（/health /auth/token /tasks …）
│   │   ├── TaskPayload.java     # 按 docs/API.md 组装 POST /tasks 的请求体
│   │   └── MainActivity.java    # 界面与流程
│   └── res/                     # 布局、文案、主题、网络安全配置、图标
├── tools/
│   ├── install-sdk.ps1          # 装最小 Android SDK（不含 Android Studio）
│   ├── make-icon.ps1            # 用 System.Drawing 生成启动图标 PNG
│   ├── SmokeTest.java           # 桌面冒烟测试（打本地桩服务器）
│   ├── AppE2E.java              # 端到端测试的 Java 驱动（打真实网关）
│   └── e2e-against-gateway.py   # 起真网关 + 跑 AppE2E 的编排脚本
└── build.ps1                    # 一键打包出 debug APK
```

`ApiClient` 和 `TaskPayload` 里**刻意不引用任何 `android.*`**，所以同一份代码能在桌面 JVM 上编译并跑测试，
不用开模拟器就能验证「请求长什么样、错误怎么解析」。

## 环境要求

- Windows + PowerShell 5.1 及以上
- JDK 17+（脚本会自动找 `JAVA_HOME`、`C:\Program Files\Eclipse Adoptium\*` 等位置）
- 能访问 `dl.google.com`（下 SDK）和 `repo1.maven.org`（下冒烟测试用的 `json.jar`）

> 改 `android\*.ps1` 时注意：这几个脚本存的是 **UTF-8 with BOM**。
> Windows PowerShell 5.1 对无 BOM 的 `.ps1` 会按系统 ANSI（简体中文机器上是 GBK）读，
> 中文会变乱码并直接报语法错。用别的编辑器另存时，请保留 BOM。

## 从零构建

```powershell
cd android

# 1) 装最小 SDK（约 130 MB 下载，装到 %LOCALAPPDATA%\Android\Sdk）
powershell -ExecutionPolicy Bypass -File tools\install-sdk.ps1

# 2) 打包
powershell -ExecutionPolicy Bypass -File build.ps1
```

产物：`android\build\qinan-agent-debug.apk`

常用开关：

```powershell
# 换 SDK 位置（已经装过 SDK 的机器）
powershell -ExecutionPolicy Bypass -File build.ps1 -SdkRoot D:\Android\Sdk

# 跳过冒烟测试、先清干净再打
powershell -ExecutionPolicy Bypass -File build.ps1 -SkipSmoke -Clean
```

### 只跑冒烟测试

不装 SDK 也能跑，只要机器上有 JDK 和一个 `org.json` 的 jar：

```powershell
$jdk = 'C:\Program Files\Eclipse Adoptium\jdk-25.0.4.101-hotspot'
$jar = 'build\libs\json-20240303.jar'   # 没有的话从 repo1.maven.org 下一个
& "$jdk\bin\javac.exe" -encoding UTF-8 -d build\smoke -cp $jar `
    app\src\main\java\com\qinan\agent\ApiClient.java `
    app\src\main\java\com\qinan\agent\TaskPayload.java tools\SmokeTest.java
& "$jdk\bin\java.exe" -cp "build\smoke;$jar" com.qinan.agent.SmokeTest
```

它会起一个本地桩服务器，检查路径、请求头（`Authorization` / `Idempotency-Key`）、请求体、
以及 401/429 的解析，最后打印「通过 N 项，失败 M 项」。

### 打真实网关的端到端测试（可选）

`SmokeTest` 打的是本机桩服务器。要验证「App 的客户端代码和真网关的契约确实对得上」，
可以在仓库根目录跑：

```powershell
.\.venv\Scripts\python.exe android\tools\e2e-against-gateway.py
```

它会用 `MOCK_MODE=true` 在 `127.0.0.1` 的随机端口上起一个真网关（不需要 opencode、
不需要模型凭据、不产生费用），建一个演示用户，然后用 `ApiClient` / `TaskPayload`
跑完整流程：鉴权 → 提交 url 任务 → 轮询 → 幂等重放 → 提交 text 任务 → 取消 →
400/404 错误码。跑完自动把网关停掉。最近一次运行：**34 项全通过**。

## 装到手机

```powershell
adb install -r android\build\qinan-agent-debug.apk
```

没有 `adb` 就把 APK 拷到手机上点安装（需要在系统里允许「安装未知来源应用」）。
debug 签名只适合自用，别拿它上应用商店。

## 界面怎么填

| 字段 | 说明 |
| --- | --- |
| 服务地址 | 网关的 `Base URL`，形如 `https://host/api/v1`（不带 `/tasks`）。结尾多个斜杠会自动去掉 |
| 信任自签证书 | 内网/演练环境用了自签证书时勾上；正式证书不用勾 |
| 鉴权 | 二选一：填账号密码点「登录并取 Token」，或直接粘一个 `access_token` |
| 任务类型 | `kind=url`（抓网页后总结）或 `kind=text`（总结你贴的一段话） |
| instruction | 可选，给智能体的额外指令，如「用三点总结，每点一行」 |
| max_output_chars | 可选，100~4000，默认 1200 |
| callback_url | 可选，必须 `https`；不填就靠 App 本地轮询拿结果 |
| client_task_id | 可选，自己业务侧的任务号，≤64 字符 |
| Idempotency-Key | 留空会自动生成 UUID，重试同一次提交时填同一个值可避免重复扣额度 |
| 原始 JSON 模式 | 勾上后完全以文本框里的 JSON 为准，用来试网关的新字段 |

发送后流程：`queued → running → streaming → succeeded/failed/canceled/timeout`。
App 前 30 秒每 2 秒查一次，之后每 5 秒查一次，最多等 5 分钟（服务端还在跑，只是本地不再等）。

## 和接口文档的对应

请求格式、状态机、错误码以 `docs/API.md` 为准。App 用到的是：

| App 里的动作 | 接口 |
| --- | --- |
| 登录并取 Token | `POST /auth/token` |
| 发送任务 | `POST /tasks`（带 `Idempotency-Key`） |
| 轮询状态/结果 | `GET /tasks/{id}` |
| 停止轮询 / 取消任务 | `POST /tasks/{id}/cancel` |

## 排错

| 现象 | 原因 |
| --- | --- |
| `[NO_BASE_URL]` | 服务地址没填 |
| `[NO_TOKEN]` | 既没登录也没粘 Token |
| `HTTP 429 [RATE_LIMITED] 被限流（不是 JSON 错误体，多半来自 nginx）` | 打到 nginx 的限流上了，不是网关的 `QUOTA_EXCEEDED` |
| `收到 HTML 而不是 JSON` | 地址/路径写错，或被前面的代理拦了 |
| `javax.net.ssl.SSLHandshakeException` | 自签证书没勾「信任自签证书」，或手机时间不对 |
| `缺少 …\build-tools\34.0.0\aapt2.exe` | 还没跑 `tools\install-sdk.ps1` |
