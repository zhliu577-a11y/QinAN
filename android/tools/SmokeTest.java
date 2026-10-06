package com.qinan.agent;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * 桌面冒烟测试：验证「发出去的请求长什么样」和「错误怎么解析」。
 *
 * 之所以能在桌面上跑，是因为 ApiClient / TaskPayload 里没有任何 android.* 引用。
 * 用法见 android/README.md，跑法：
 *   javac -encoding UTF-8 -d build/smoke app/src/main/java/com/qinan/agent/*.java tools/SmokeTest.java
 *   java -cp "build/smoke;<android.jar>" com.qinan.agent.SmokeTest
 */
public final class SmokeTest {

    private static int passed = 0;
    private static int failed = 0;

    // 桩服务器上记录到的最后一次请求
    private static volatile String lastMethod = "";
    private static volatile String lastPath = "";
    private static volatile String lastBody = "";
    private static final Map<String, String> lastHeaders = new HashMap<>();

    // POST /tasks 的可调响应
    private static volatile int tasksStatus = 201;
    private static volatile String tasksBody =
            "{\"task_id\":\"t-1\",\"status\":\"queued\",\"queue_pos\":0,"
                    + "\"estimated_wait_seconds\":3,\"created_at\":\"2026-01-01T00:00:00Z\"}";
    private static volatile String tasksExtraHeaderName = null;
    private static volatile String tasksExtraHeaderValue = null;

    public static void main(String[] args) throws Exception {
        testPayloadUrl();
        testPayloadText();
        testPayloadOptionalFields();
        testValidate();
        testTerminal();
        testStripTrailingSlash();
        testErrorParsing();

        HttpServer server = startStubServer();
        String base = "http://127.0.0.1:" + server.getAddress().getPort() + "/api/v1";
        try {
            testHealth(base);
            testNoToken(base);
            testLogin(base);
            testCreateTask(base + "/");
            testGetTask(base);
            testCancelTask(base);
            testHttpErrorFromServer(base);
        } finally {
            server.stop(0);
        }

        System.out.println();
        System.out.println("通过 " + passed + " 项，失败 " + failed + " 项");
        if (failed > 0) {
            System.exit(1);
        }
    }

    // ---------- 请求体组装 ----------

    private static void testPayloadUrl() throws Exception {
        TaskPayload payload = TaskPayload.forUrl();
        payload.url = "  https://example.com/a?b=1  ";
        payload.maxOutputChars = "1200";
        JSONObject body = payload.toJson();

        check("kind=url 时 kind 字段", "url".equals(body.getString("kind")));
        check("kind=url 时 url 会被 trim", "https://example.com/a?b=1".equals(body.getString("url")));
        check("kind=url 时不带 text", !body.has("text"));
        check("max_output_chars 是数字而不是字符串", body.opt("max_output_chars") instanceof Integer);
        check("max_output_chars=1200", body.optInt("max_output_chars") == 1200);
        check("没有值的可选字段不上送", !body.has("instruction") && !body.has("callback_url")
                && !body.has("client_task_id"));
        check("url 模式的校验通过", payload.validate() == null);
    }

    private static void testPayloadText() throws Exception {
        TaskPayload payload = TaskPayload.forText();
        payload.text = "第一段\n第二段";
        payload.instruction = "用三点总结";
        JSONObject body = payload.toJson();

        check("kind=text 时 kind 字段", "text".equals(body.getString("kind")));
        check("kind=text 时正文保留换行", "第一段\n第二段".equals(body.getString("text")));
        check("kind=text 时不带 url", !body.has("url"));
        check("instruction 上送", "用三点总结".equals(body.getString("instruction")));
    }

    private static void testPayloadOptionalFields() throws Exception {
        TaskPayload payload = TaskPayload.forUrl();
        payload.url = "https://example.com";
        payload.callbackUrl = " https://cb.example.com/hook ";
        payload.clientTaskId = "  my-id-1  ";
        payload.maxOutputChars = "   ";
        JSONObject body = payload.toJson();

        check("callback_url 上送且 trim", "https://cb.example.com/hook".equals(body.getString("callback_url")));
        check("client_task_id 上送且 trim", "my-id-1".equals(body.getString("client_task_id")));
        check("max_output_chars 留空时不上送", !body.has("max_output_chars"));
    }

    private static void testValidate() {
        check("空 url 被拦下", expectProblem(TaskPayload.forUrl(), "URL 不能为空"));

        TaskPayload badScheme = TaskPayload.forUrl();
        badScheme.url = "example.com";
        check("非 http(s) 被拦下", badScheme.validate() != null);

        TaskPayload longUrl = TaskPayload.forUrl();
        longUrl.url = "https://example.com/" + repeat("a", 2048);
        check("超长 url 被拦下", longUrl.validate() != null);

        TaskPayload emptyText = TaskPayload.forText();
        emptyText.text = "   ";
        check("空正文被拦下", emptyText.validate() != null);

        TaskPayload tooSmall = TaskPayload.forUrl();
        tooSmall.url = "https://example.com";
        tooSmall.maxOutputChars = "99";
        check("max_output_chars 过小被拦下", tooSmall.validate() != null);

        TaskPayload tooBig = TaskPayload.forUrl();
        tooBig.url = "https://example.com";
        tooBig.maxOutputChars = "4001";
        check("max_output_chars 过大被拦下", tooBig.validate() != null);

        TaskPayload notNumber = TaskPayload.forUrl();
        notNumber.url = "https://example.com";
        notNumber.maxOutputChars = "abc";
        check("max_output_chars 非数字被拦下", notNumber.validate() != null);

        TaskPayload longInstruction = TaskPayload.forUrl();
        longInstruction.url = "https://example.com";
        longInstruction.instruction = repeat("x", 4001);
        check("超长 instruction 被拦下", longInstruction.validate() != null);

        TaskPayload longClientId = TaskPayload.forUrl();
        longClientId.url = "https://example.com";
        longClientId.clientTaskId = repeat("x", 65);
        check("超长 client_task_id 被拦下", longClientId.validate() != null);

        TaskPayload httpCallback = TaskPayload.forUrl();
        httpCallback.url = "https://example.com";
        httpCallback.callbackUrl = "http://cb.example.com/hook";
        check("非 https 的 callback_url 被拦下", httpCallback.validate() != null);
    }

    private static void testTerminal() {
        check("succeeded 是终态", TaskPayload.isTerminal("succeeded"));
        check("failed 是终态", TaskPayload.isTerminal("failed"));
        check("canceled 是终态", TaskPayload.isTerminal("canceled"));
        check("timeout 是终态", TaskPayload.isTerminal("timeout"));
        check("queued 不是终态", !TaskPayload.isTerminal("queued"));
        check("running 不是终态", !TaskPayload.isTerminal("running"));
        check("streaming 不是终态", !TaskPayload.isTerminal("streaming"));
    }

    private static void testStripTrailingSlash() {
        check("去掉结尾斜杠", "https://h/api/v1".equals(ApiClient.stripTrailingSlash("https://h/api/v1/")));
        check("去掉多个结尾斜杠", "https://h/api/v1".equals(ApiClient.stripTrailingSlash("https://h/api/v1///")));
        check("空串不炸", "".equals(ApiClient.stripTrailingSlash("")));
    }

    // ---------- 错误解析 ----------

    private static void testErrorParsing() {
        Map<String, String> withRetryAfter = new HashMap<>();
        withRetryAfter.put("Retry-After", "30");

        ApiClient.ApiException quota = ApiClient.toException(new ApiClient.Response(429,
                "{\"error\":{\"code\":\"QUOTA_EXCEEDED\",\"message\":\"今日额度用完\",\"retry_after\":30}}",
                new HashMap<>()));
        check("JSON 429 → code=QUOTA_EXCEEDED", "QUOTA_EXCEEDED".equals(quota.code));
        check("JSON 429 → 带 retryAfter", quota.retryAfter != null && quota.retryAfter == 30);
        check("JSON 429 → 保留 message", "今日额度用完".equals(quota.getMessage()));

        ApiClient.ApiException html429 = ApiClient.toException(new ApiClient.Response(429,
                "<html><head><title>429 Too Many Requests</title></head></html>", withRetryAfter));
        check("HTML 429 → 识别为 nginx 限流", "RATE_LIMITED".equals(html429.code));
        check("HTML 429 → 从响应头读到 Retry-After=30",
                html429.retryAfter != null && html429.retryAfter == 30);

        ApiClient.ApiException html500 = ApiClient.toException(new ApiClient.Response(500,
                "<html>Internal Server Error</html>", new HashMap<>()));
        check("HTML 500 → 提示是 HTML 不是 JSON", html500.getMessage().contains("HTML"));

        ApiClient.ApiException empty = ApiClient.toException(new ApiClient.Response(502, "", new HashMap<>()));
        check("空响应体 → 兜底 code=BAD_RESPONSE", "BAD_RESPONSE".equals(empty.code));

        ApiClient.ApiException unauthorized = ApiClient.toException(new ApiClient.Response(401,
                "{\"error\":{\"code\":\"UNAUTHORIZED\",\"message\":\"token 无效\"}}", new HashMap<>()));
        check("401 → code=UNAUTHORIZED", "UNAUTHORIZED".equals(unauthorized.code));
        check("describe() 带上 HTTP 状态码", unauthorized.describe().contains("HTTP 401"));
    }

    // ---------- 走真实 HTTP ----------

    private static void testHealth(String base) throws Exception {
        ApiClient client = new ApiClient(base, false);
        ApiClient.Response response = client.health();
        JSONObject body = response.json();
        check("GET /health 返回 200", response.status == 200);
        check("健康检查路径不带鉴权", "/api/v1/health".equals(lastPath));
        check("健康检查能读到 pool_total", body.optInt("pool_total") == 2);
    }

    private static void testNoToken(String base) {
        ApiClient client = new ApiClient(base, false);
        try {
            client.createTask(new JSONObject(), "k1");
            check("没 Token 时应当报错", false);
        } catch (ApiClient.ApiException exc) {
            check("没 Token 时本地就拦下", "NO_TOKEN".equals(exc.code));
        } catch (Exception exc) {
            check("没 Token 时应当报错而不是 " + exc.getClass().getSimpleName(), false);
        }
        try {
            new ApiClient("", false).health();
            check("没填地址时应当报错", false);
        } catch (ApiClient.ApiException exc) {
            check("没填地址时报 NO_BASE_URL", "NO_BASE_URL".equals(exc.code));
        } catch (Exception exc) {
            check("没填地址时应当报错而不是 " + exc.getClass().getSimpleName(), false);
        }
    }

    private static void testLogin(String base) throws Exception {
        ApiClient client = new ApiClient(base, false);
        JSONObject result = client.login("admin", "secret", "android-app");
        check("POST /auth/token 路径", "/api/v1/auth/token".equals(lastPath));
        check("登录请求体带 username", lastBody.contains("\"username\":\"admin\""));
        check("登录请求体带 device_id", lastBody.contains("\"device_id\":\"android-app\""));
        check("登录后自动装上 token", "tok-123".equals(client.getToken()));
        check("能读到 user.daily_quota", result.getJSONObject("user").optInt("daily_quota") == 50);
    }

    private static void testCreateTask(String baseWithSlash) throws Exception {
        ApiClient client = new ApiClient(baseWithSlash, false);
        client.setToken("tok-123");

        TaskPayload payload = TaskPayload.forUrl();
        payload.url = "https://example.com/news";
        payload.instruction = "总结三点";
        JSONObject created = client.createTask(payload.toJson(), "idem-1");

        check("POST /tasks 路径（baseUrl 结尾斜杠不会拼出双斜杠）",
                "/api/v1/tasks".equals(lastPath));
        check("带上 Authorization 头", "Bearer tok-123".equals(header("authorization")));
        check("带上 Idempotency-Key 头", "idem-1".equals(header("idempotency-key")));
        check("Content-Type 是 application/json",
                header("content-type") != null && header("content-type").startsWith("application/json"));
        check("请求体是按 Content-Length 发出去的，不是 chunked",
                header("transfer-encoding") == null);
        check("请求体里 kind=url", lastBody.contains("\"kind\":\"url\""));
        check("请求体里带 instruction", lastBody.contains("\"instruction\":\"总结三点\""));
        check("能读到 task_id", "t-1".equals(created.optString("task_id")));
        check("能读到 queue_pos", created.optInt("queue_pos") == 0);
    }

    private static void testGetTask(String base) throws Exception {
        ApiClient client = new ApiClient(base, false);
        client.setToken("tok-123");
        JSONObject detail = client.getTask("t-1");

        check("GET /tasks/{id} 路径", "/api/v1/tasks/t-1".equals(lastPath));
        check("GET 不带请求体", lastBody.isEmpty());
        check("状态是 succeeded", "succeeded".equals(detail.optString("status")));
        check("能读到 result_md", detail.optString("result_md").startsWith("# 摘要"));
        check("能读到 usage.tokens_out", detail.getJSONObject("usage").optInt("tokens_out") == 20);
        check("能读到 source.fetched_chars",
                detail.getJSONObject("source").optInt("fetched_chars") == 4321);
        check("succeeded 是终态", TaskPayload.isTerminal(detail.optString("status")));
    }

    private static void testCancelTask(String base) throws Exception {
        ApiClient client = new ApiClient(base, false);
        client.setToken("tok-123");
        JSONObject result = client.cancelTask("t-1");
        check("POST /tasks/{id}/cancel 路径", "/api/v1/tasks/t-1/cancel".equals(lastPath));
        check("取消后状态是 canceled", "canceled".equals(result.optString("status")));
    }

    private static void testHttpErrorFromServer(String base) throws Exception {
        ApiClient client = new ApiClient(base, false);
        client.setToken("tok-123");

        tasksStatus = 401;
        tasksBody = "{\"error\":{\"code\":\"UNAUTHORIZED\",\"message\":\"token 已过期\"}}";
        tasksExtraHeaderName = null;
        try {
            client.createTask(new JSONObject(), "idem-2");
            check("401 应当抛 ApiException", false);
        } catch (ApiClient.ApiException exc) {
            check("401 抛 ApiException 且 httpStatus=401", exc.httpStatus == 401);
            check("401 解析出 code=UNAUTHORIZED", "UNAUTHORIZED".equals(exc.code));
        }

        tasksStatus = 429;
        tasksBody = "<html><head><title>429 Too Many Requests</title></head></html>";
        tasksExtraHeaderName = "Retry-After";
        tasksExtraHeaderValue = "12";
        try {
            client.createTask(new JSONObject(), "idem-3");
            check("429 应当抛 ApiException", false);
        } catch (ApiClient.ApiException exc) {
            check("HTML 429 抛 ApiException 且 httpStatus=429", exc.httpStatus == 429);
            check("HTML 429 解析出 RATE_LIMITED", "RATE_LIMITED".equals(exc.code));
            check("HTML 429 读到 Retry-After=12", exc.retryAfter != null && exc.retryAfter == 12);
        }

        tasksStatus = 201;
        tasksBody = "{\"task_id\":\"t-1\",\"status\":\"queued\",\"queue_pos\":0,"
                + "\"estimated_wait_seconds\":3,\"created_at\":\"2026-01-01T00:00:00Z\"}";
        tasksExtraHeaderName = null;
        tasksExtraHeaderValue = null;
    }

    // ---------- 桩服务器 ----------

    private static HttpServer startStubServer() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", SmokeTest::handle);
        server.setExecutor(null);
        server.start();
        return server;
    }

    private static void handle(HttpExchange exchange) throws java.io.IOException {
        lastMethod = exchange.getRequestMethod();
        lastPath = exchange.getRequestURI().getPath();
        lastBody = readAll(exchange.getRequestBody());
        lastHeaders.clear();
        for (Map.Entry<String, java.util.List<String>> entry : exchange.getRequestHeaders().entrySet()) {
            if (!entry.getValue().isEmpty()) {
                lastHeaders.put(entry.getKey(), entry.getValue().get(0));
            }
        }

        int status;
        String body;
        String extraHeaderName = null;
        String extraHeaderValue = null;
        if (lastPath.endsWith("/health")) {
            status = 200;
            body = "{\"status\":\"ok\",\"engine\":\"opencode\",\"pool_idle\":1,"
                    + "\"pool_total\":2,\"queue_len\":0}";
        } else if (lastPath.endsWith("/auth/token")) {
            status = 200;
            body = "{\"access_token\":\"tok-123\",\"token_type\":\"bearer\",\"expires_in\":3600,"
                    + "\"refresh_token\":\"ref-1\",\"user\":{\"id\":\"u1\",\"username\":\"admin\","
                    + "\"display_name\":\"管理员\",\"daily_quota\":50}}";
        } else if (lastPath.endsWith("/tasks")) {
            status = tasksStatus;
            body = tasksBody;
            extraHeaderName = tasksExtraHeaderName;
            extraHeaderValue = tasksExtraHeaderValue;
        } else if (lastPath.endsWith("/cancel")) {
            status = 200;
            body = "{\"task_id\":\"t-1\",\"status\":\"canceled\"}";
        } else if (lastPath.contains("/tasks/")) {
            status = 200;
            body = "{\"task_id\":\"t-1\",\"client_task_id\":\"my-id-1\",\"kind\":\"url\","
                    + "\"status\":\"succeeded\",\"queue_pos\":0,"
                    + "\"result_md\":\"# 摘要\\n\\n- 一\\n- 二\","
                    + "\"source\":{\"url\":\"https://example.com/news\",\"title\":\"新闻\","
                    + "\"fetched_chars\":4321},"
                    + "\"usage\":{\"tokens_in\":100,\"tokens_out\":20,\"duration_ms\":1234},"
                    + "\"error\":null,\"created_at\":\"2026-01-01T00:00:00Z\","
                    + "\"started_at\":\"2026-01-01T00:00:01Z\",\"finished_at\":\"2026-01-01T00:00:03Z\"}";
        } else {
            status = 404;
            body = "{\"error\":{\"code\":\"NOT_FOUND\",\"message\":\"no such route\"}}";
        }

        if (extraHeaderName != null) {
            exchange.getResponseHeaders().set(extraHeaderName, extraHeaderValue);
        }
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        byte[] raw = body.getBytes(StandardCharsets.UTF_8);
        exchange.sendResponseHeaders(status, raw.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(raw);
        }
    }

    private static String readAll(InputStream stream) throws java.io.IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        byte[] chunk = new byte[4096];
        int read;
        while ((read = stream.read(chunk)) != -1) {
            buffer.write(chunk, 0, read);
        }
        return new String(buffer.toByteArray(), StandardCharsets.UTF_8);
    }

    // ---------- 断言 ----------

    private static boolean expectProblem(TaskPayload payload, String expectedFragment) {
        String problem = payload.validate();
        if (problem == null) {
            return false;
        }
        return problem.contains(expectedFragment);
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  [ok]   " + name);
        } else {
            failed++;
            System.out.println("  [FAIL] " + name);
        }
    }

    private static String repeat(String unit, int times) {
        StringBuilder sb = new StringBuilder(unit.length() * times);
        for (int i = 0; i < times; i++) {
            sb.append(unit);
        }
        return sb.toString();
    }

    /** HTTP 头名大小写不敏感：服务端 JDK 会把 Content-Type 记成 Content-type。 */
    private static String header(String lowerCaseName) {
        for (Map.Entry<String, String> entry : lastHeaders.entrySet()) {
            if (entry.getKey() != null && entry.getKey().toLowerCase(java.util.Locale.US).equals(lowerCaseName)) {
                return entry.getValue();
            }
        }
        return null;
    }

    private SmokeTest() {
    }
}
