package com.qinan.agent;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.cert.X509Certificate;
import java.util.LinkedHashMap;
import java.util.Map;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * 网关客户端。
 *
 * 这个类只用 java.net / java.nio / org.json，**不引用任何 android.* 类**，
 * 因此同一份代码可以在桌面上编译并跑冒烟测试（见 android/tools/SmokeTest.java），
 * 不用开模拟器就能验证请求格式与接口路径是对的。
 */
public class ApiClient {

    /** 网关返回的错误：{"error":{"code":..,"message":..,"retry_after":..}} 或非 JSON 的 HTML。 */
    public static class ApiException extends Exception {
        public final int httpStatus;
        public final String code;
        public final Integer retryAfter;

        public ApiException(int httpStatus, String code, String message, Integer retryAfter) {
            super(message);
            this.httpStatus = httpStatus;
            this.code = code;
            this.retryAfter = retryAfter;
        }

        public String describe() {
            StringBuilder sb = new StringBuilder();
            if (httpStatus > 0) {
                sb.append("HTTP ").append(httpStatus).append(' ');
            }
            sb.append('[').append(code).append("] ").append(getMessage());
            if (retryAfter != null) {
                sb.append("（建议 ").append(retryAfter).append(" 秒后重试）");
            }
            return sb.toString();
        }
    }

    public static class Response {
        public final int status;
        public final String body;
        public final Map<String, String> headers;

        Response(int status, String body, Map<String, String> headers) {
            this.status = status;
            this.body = body;
            this.headers = headers;
        }

        public JSONObject json() throws JSONException {
            return new JSONObject(body);
        }

        /**
         * 按名字取响应头，大小写不敏感。
         * 注意 JDK 会把服务端的 Retry-After 规范化成 Retry-after，直接 get 是取不到的。
         */
        public String header(String name) {
            if (headers == null) {
                return null;
            }
            for (Map.Entry<String, String> entry : headers.entrySet()) {
                if (entry.getKey() != null && entry.getKey().equalsIgnoreCase(name)) {
                    return entry.getValue();
                }
            }
            return null;
        }
    }

    private static final String USER_AGENT = "QinAN-Android/1.0";

    private final String baseUrl;
    private final boolean trustAllCerts;
    private String token;
    private int connectTimeoutMs = 15000;
    private int readTimeoutMs = 60000;

    public ApiClient(String baseUrl, boolean trustAllCerts) {
        this.baseUrl = stripTrailingSlash(baseUrl == null ? "" : baseUrl.trim());
        this.trustAllCerts = trustAllCerts;
    }

    public String getBaseUrl() {
        return baseUrl;
    }

    public String getToken() {
        return token;
    }

    public void setToken(String token) {
        this.token = token;
    }

    public void setConnectTimeoutMs(int ms) {
        this.connectTimeoutMs = ms;
    }

    public void setReadTimeoutMs(int ms) {
        this.readTimeoutMs = ms;
    }

    // ---------- 接口 ----------

    /** GET /health（不需要鉴权）。 */
    public Response health() throws IOException, ApiException {
        return request("GET", "/health", null, null, false);
    }

    /** POST /auth/token：模式 A，账号密码换 Token。 */
    public JSONObject login(String username, String password, String deviceId)
            throws IOException, ApiException, JSONException {
        JSONObject body = new JSONObject();
        body.put("username", username);
        body.put("password", password);
        if (deviceId != null && !deviceId.isEmpty()) {
            body.put("device_id", deviceId);
        }
        JSONObject result = request("POST", "/auth/token", body, null, false).json();
        String accessToken = result.optString("access_token", "");
        if (accessToken.isEmpty()) {
            throw new ApiException(0, "NO_TOKEN", "登录成功但响应里没有 access_token", null);
        }
        this.token = accessToken;
        return result;
    }

    /** POST /tasks：提交任务。body 由调用方组装（支持界面上手改原始 JSON）。 */
    public JSONObject createTask(JSONObject body, String idempotencyKey)
            throws IOException, ApiException, JSONException {
        Map<String, String> headers = new LinkedHashMap<>();
        if (idempotencyKey != null && !idempotencyKey.isEmpty()) {
            headers.put("Idempotency-Key", idempotencyKey);
        }
        return request("POST", "/tasks", body, headers, true).json();
    }

    /** GET /tasks/{id}：查状态与结果。 */
    public JSONObject getTask(String taskId) throws IOException, ApiException, JSONException {
        return request("GET", "/tasks/" + taskId, null, null, true).json();
    }

    /** POST /tasks/{id}/cancel：取消任务。 */
    public JSONObject cancelTask(String taskId) throws IOException, ApiException, JSONException {
        return request("POST", "/tasks/" + taskId + "/cancel", null, null, true).json();
    }

    // ---------- HTTP ----------

    private Response request(
            String method, String path, JSONObject body, Map<String, String> extraHeaders, boolean auth)
            throws IOException, ApiException {
        if (baseUrl.isEmpty()) {
            throw new ApiException(0, "NO_BASE_URL", "还没填服务地址（Base URL）", null);
        }
        if (auth && (token == null || token.isEmpty())) {
            throw new ApiException(0, "NO_TOKEN", "还没有 Token，请先登录或直接填 Token", null);
        }

        URL url = new URL(baseUrl + path);
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        try {
            applyTls(conn);
            conn.setRequestMethod(method);
            conn.setConnectTimeout(connectTimeoutMs);
            conn.setReadTimeout(readTimeoutMs);
            conn.setRequestProperty("Accept", "application/json");
            conn.setRequestProperty("User-Agent", USER_AGENT);
            if (auth) {
                conn.setRequestProperty("Authorization", "Bearer " + token);
            }
            if (extraHeaders != null) {
                for (Map.Entry<String, String> entry : extraHeaders.entrySet()) {
                    conn.setRequestProperty(entry.getKey(), entry.getValue());
                }
            }

            if (body != null) {
                byte[] raw = body.toString().getBytes(StandardCharsets.UTF_8);
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                // 刻意不用 setFixedLengthStreamingMode / setChunkedStreamingMode：
                // 一旦进了流式模式，JDK 收到 401 时会抛
                // HttpRetryException("cannot retry due to server authentication, in streaming mode")，
                // getErrorStream() 变成 null，网关返回的 {"error":{"code":"UNAUTHORIZED"}} 就读不到了。
                // 不带长度时 JDK 会先把请求体缓存起来（Content-Length 自己算），401 的响应体才读得到。
                // 请求体上限只有几百 KB（正文 ≤200000 字符），缓存这点内存无所谓。
                try (OutputStream out = conn.getOutputStream()) {
                    out.write(raw);
                }
            }

            int status = conn.getResponseCode();
            InputStream stream = readStream(conn, status);
            String text = stream == null ? "" : readAll(stream);

            Response response = new Response(status, text, collectHeaders(conn));
            if (status >= 400) {
                throw toException(response);
            }
            return response;
        } finally {
            conn.disconnect();
        }
    }

    /**
     * 把非 2xx 折成 ApiException。
     * 注意要能区分两种 429：网关的 JSON（带 code / Retry-After）与 nginx 的 HTML 错误页。
     */
    static ApiException toException(Response response) {
        String body = response.body == null ? "" : response.body.trim();
        Integer retryAfter = parseRetryAfter(response.header("Retry-After"));
        if (body.startsWith("{")) {
            try {
                JSONObject error = new JSONObject(body).optJSONObject("error");
                if (error != null) {
                    String code = error.optString("code", "UNKNOWN");
                    String message = error.optString("message", "");
                    if (retryAfter == null && error.has("retry_after")) {
                        retryAfter = error.optInt("retry_after");
                    }
                    return new ApiException(response.status, code, message, retryAfter);
                }
            } catch (JSONException ignored) {
                // 落到下面的兜底分支
            }
        }
        String hint = body.isEmpty() ? "" : "，响应内容：" + shorten(body, 200);
        String message;
        if (response.status == 429) {
            message = "被限流（不是 JSON 错误体，多半来自 nginx）" + hint;
        } else if (body.startsWith("<")) {
            message = "收到 HTML 而不是 JSON（多半是域名/路径不对，或被网关前面的代理拦了）" + hint;
        } else {
            message = "响应不是预期的 JSON" + hint;
        }
        return new ApiException(response.status, response.status == 429 ? "RATE_LIMITED" : "BAD_RESPONSE",
                message, retryAfter);
    }

    /** 取响应体：2xx 用 inputStream，4xx/5xx 优先 errorStream。 */
    private static InputStream readStream(HttpURLConnection conn, int status) throws IOException {
        if (status < 400) {
            return conn.getInputStream();
        }
        InputStream stream = conn.getErrorStream();
        if (stream != null) {
            return stream;
        }
        // 个别 JVM/场景下（例如 401 被自动重试逻辑吃掉）errorStream 会是 null，退回试 inputStream。
        try {
            return conn.getInputStream();
        } catch (IOException ignored) {
            return null;
        }
    }

    private void applyTls(HttpURLConnection conn) {
        if (!trustAllCerts || !(conn instanceof HttpsURLConnection)) {
            return;
        }
        HttpsURLConnection https = (HttpsURLConnection) conn;
        try {
            SSLContext context = SSLContext.getInstance("TLS");
            context.init(null, new TrustManager[]{TRUST_ALL}, new java.security.SecureRandom());
            SSLSocketFactory factory = context.getSocketFactory();
            https.setSSLSocketFactory(factory);
            https.setHostnameVerifier((hostname, session) -> true);
        } catch (Exception exc) {
            throw new IllegalStateException("无法配置「信任自签名证书」: " + exc.getMessage(), exc);
        }
    }

    private static final X509TrustManager TRUST_ALL = new X509TrustManager() {
        @Override
        public void checkClientTrusted(X509Certificate[] chain, String authType) {
        }

        @Override
        public void checkServerTrusted(X509Certificate[] chain, String authType) {
        }

        @Override
        public X509Certificate[] getAcceptedIssuers() {
            return new X509Certificate[0];
        }
    };

    private static Map<String, String> collectHeaders(HttpURLConnection conn) {
        Map<String, String> headers = new LinkedHashMap<>();
        for (Map.Entry<String, java.util.List<String>> entry : conn.getHeaderFields().entrySet()) {
            if (entry.getKey() != null && !entry.getValue().isEmpty()) {
                headers.put(entry.getKey(), entry.getValue().get(0));
            }
        }
        return headers;
    }

    private static Integer parseRetryAfter(String value) {
        if (value == null || value.isEmpty()) {
            return null;
        }
        try {
            return Integer.valueOf(value.trim());
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    private static String readAll(InputStream stream) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        byte[] chunk = new byte[8192];
        int read;
        while ((read = stream.read(chunk)) != -1) {
            buffer.write(chunk, 0, read);
        }
        return new String(buffer.toByteArray(), StandardCharsets.UTF_8);
    }

    private static String shorten(String text, int max) {
        return text.length() <= max ? text : text.substring(0, max) + "…";
    }

    static String stripTrailingSlash(String url) {
        String result = url;
        while (result.endsWith("/")) {
            result = result.substring(0, result.length() - 1);
        }
        return result;
    }
}
