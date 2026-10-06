package com.qinan.agent;

import org.json.JSONException;
import org.json.JSONObject;

/**
 * 按 docs/API.md 组装 POST /tasks 的请求体。
 *
 * 也是纯 Java（无 android.*），所以桌面上就能验证"发出去的 JSON 长什么样"。
 */
public final class TaskPayload {

    public static final String KIND_URL = "url";
    public static final String KIND_TEXT = "text";

    public String kind = KIND_TEXT;
    public String url = "";
    public String text = "";
    public String instruction = "";
    public String maxOutputChars = "1200";
    public String callbackUrl = "";
    public String clientTaskId = "";

    private TaskPayload() {
    }

    public static TaskPayload forUrl() {
        TaskPayload payload = new TaskPayload();
        payload.kind = KIND_URL;
        return payload;
    }

    public static TaskPayload forText() {
        TaskPayload payload = new TaskPayload();
        payload.kind = KIND_TEXT;
        return payload;
    }

    /** 只带上真正有值的字段，避免把空串发上去。 */
    public JSONObject toJson() throws JSONException {
        JSONObject body = new JSONObject();
        body.put("kind", kind);
        if (KIND_URL.equals(kind)) {
            body.put("url", url == null ? "" : url.trim());
        } else {
            body.put("text", text == null ? "" : text);
        }
        putIfNotEmpty(body, "instruction", instruction);
        Integer max = parsePositiveInt(maxOutputChars);
        if (max != null) {
            body.put("max_output_chars", max.intValue());
        }
        putIfNotEmpty(body, "callback_url", callbackUrl);
        putIfNotEmpty(body, "client_task_id", clientTaskId);
        return body;
    }

    /** 提交前的本地校验：能在本地拦下来的错就不要浪费一次网络往返。 */
    public String validate() {
        if (KIND_URL.equals(kind)) {
            String value = url == null ? "" : url.trim();
            if (value.isEmpty()) {
                return "kind=url 时 URL 不能为空";
            }
            if (!value.startsWith("http://") && !value.startsWith("https://")) {
                return "URL 必须以 http:// 或 https:// 开头";
            }
            if (value.length() > 2048) {
                return "URL 长度超过上限 2048";
            }
        } else {
            String value = text == null ? "" : text;
            if (value.trim().isEmpty()) {
                return "kind=text 时正文不能为空";
            }
            if (value.length() > 200000) {
                return "正文长度 " + value.length() + " 超过上限 200000";
            }
        }
        Integer max = parsePositiveInt(maxOutputChars);
        if (!maxOutputChars.trim().isEmpty() && max == null) {
            return "max_output_chars 必须是正整数";
        }
        if (max != null && (max.intValue() < 100 || max.intValue() > 4000)) {
            return "max_output_chars 必须在 100~4000 之间";
        }
        if (instruction != null && instruction.length() > 4000) {
            return "instruction 长度超过上限 4000";
        }
        if (clientTaskId != null && clientTaskId.length() > 64) {
            return "client_task_id 长度超过上限 64";
        }
        if (callbackUrl != null && !callbackUrl.trim().isEmpty()
                && !callbackUrl.trim().startsWith("https://")) {
            return "callback_url 必须是 https";
        }
        return null;
    }

    public static boolean isTerminal(String status) {
        return "succeeded".equals(status)
                || "failed".equals(status)
                || "canceled".equals(status)
                || "timeout".equals(status);
    }

    private static void putIfNotEmpty(JSONObject body, String key, String value) throws JSONException {
        if (value != null && !value.trim().isEmpty()) {
            body.put(key, value.trim());
        }
    }

    private static Integer parsePositiveInt(String raw) {
        if (raw == null || raw.trim().isEmpty()) {
            return null;
        }
        try {
            int value = Integer.parseInt(raw.trim());
            return value > 0 ? Integer.valueOf(value) : null;
        } catch (NumberFormatException ignored) {
            return null;
        }
    }
}
