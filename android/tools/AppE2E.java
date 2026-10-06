package com.qinan.agent;

import org.json.JSONObject;

/**
 * 端到端验证：用 App 里那份 ApiClient / TaskPayload 去打**真实网关**。
 *
 * 和 SmokeTest 的分工：
 *   - SmokeTest 打本地桩服务器，验证「请求长什么样、错误怎么解析」，不需要网关；
 *   - 这个打真网关，验证「接口契约真的对得上」，需要网关在跑。
 *
 * 由 tools/e2e-against-gateway.py 调起，一般不用自己跑。参数：
 *   java com.qinan.agent.AppE2E <baseUrl> <username> <password>
 */
public final class AppE2E {

    private static int ok = 0;
    private static int bad = 0;

    public static void main(String[] args) throws Exception {
        String base = args[0];
        String username = args[1];
        String password = args[2];

        // 1. /health 免鉴权
        ApiClient anon = new ApiClient(base, false);
        ApiClient.Response health = anon.health();
        JSONObject healthBody = health.json();
        check("GET /health → 200", health.status == 200);
        check("health.status = ok", "ok".equals(healthBody.optString("status")));
        check("health 带 engine / pool_total / queue_len",
                healthBody.has("engine") && healthBody.has("pool_total") && healthBody.has("queue_len"));
        System.out.println("       health = " + healthBody);

        // 2. 错密码 → 401
        try {
            new ApiClient(base, false).login(username, "definitely-wrong-password", "android-app");
            check("错密码应当被拒", false);
        } catch (ApiClient.ApiException exc) {
            check("错密码 → HTTP 401", exc.httpStatus == 401);
            check("错密码 → code=UNAUTHORIZED", "UNAUTHORIZED".equals(exc.code));
            System.out.println("       错密码 = " + exc.describe());
        }

        // 3. 正确登录
        ApiClient client = new ApiClient(base, false);
        JSONObject login = client.login(username, password, "android-app");
        check("登录拿到 access_token", client.getToken() != null && !client.getToken().isEmpty());
        check("登录返回 user.daily_quota", login.getJSONObject("user").optInt("daily_quota") > 0);
        check("登录返回 expires_in / refresh_token",
                login.optInt("expires_in") > 0 && !login.optString("refresh_token").isEmpty());

        // 4. 没 token 提交任务
        try {
            new ApiClient(base, false)
                    .createTask(new JSONObject().put("kind", "text").put("text", "hi"), "e2e-notoken");
            check("没 token 应当被拒", false);
        } catch (ApiClient.ApiException exc) {
            check("没 token → 本地就拦下（NO_TOKEN）", "NO_TOKEN".equals(exc.code));
        }

        // 5. 本地校验：坏 url 不该走到网络
        TaskPayload badUrl = TaskPayload.forUrl();
        badUrl.url = "ftp://example.com";
        check("坏 url 在本地被 validate() 拦下", badUrl.validate() != null);

        // 6. kind=url 全流程
        TaskPayload urlPayload = TaskPayload.forUrl();
        urlPayload.url = "https://example.com/news/2026";
        urlPayload.instruction = "用三点总结，每点一行";
        urlPayload.maxOutputChars = "800";
        urlPayload.clientTaskId = "e2e-url-1";
        JSONObject urlDetail = submitAndWait(client, urlPayload, "e2e-idem-url-1");
        check("url 任务拿到终态", urlDetail != null);
        if (urlDetail != null) {
            check("url 任务 succeeded", "succeeded".equals(urlDetail.optString("status")));
            check("url 任务回填 client_task_id", "e2e-url-1".equals(urlDetail.optString("client_task_id")));
            check("url 任务 kind=url", "url".equals(urlDetail.optString("kind")));
            check("url 任务有 result_md", !urlDetail.optString("result_md", "").isEmpty());
            check("url 任务有 usage", urlDetail.optJSONObject("usage") != null);
            check("url 任务有 finished_at", !urlDetail.optString("finished_at", "").isEmpty());
            System.out.println("       result_md 前 80 字 = "
                    + first(urlDetail.optString("result_md"), 80).replace("\n", " "));
        }

        // 7. 幂等：同一个 Idempotency-Key 再提一次，应当返回同一个 task_id
        JSONObject replay = client.createTask(urlPayload.toJson(), "e2e-idem-url-1");
        check("同 Idempotency-Key 重放 → 同一个 task_id",
                urlDetail != null && urlDetail.optString("task_id").equals(replay.optString("task_id")));

        // 8. kind=text 全流程
        TaskPayload textPayload = TaskPayload.forText();
        textPayload.text = "第一段内容。\n第二段内容，包含了三句话。\n第三段是结论。";
        textPayload.instruction = "压缩成一句话";
        JSONObject textDetail = submitAndWait(client, textPayload, "e2e-idem-text-1");
        check("text 任务拿到终态", textDetail != null);
        if (textDetail != null) {
            check("text 任务 succeeded", "succeeded".equals(textDetail.optString("status")));
            check("text 任务 kind=text", "text".equals(textDetail.optString("kind")));
            check("text 任务有 result_md", !textDetail.optString("result_md", "").isEmpty());
        }

        // 9. 取消：提交后立刻 cancel
        TaskPayload cancelPayload = TaskPayload.forUrl();
        cancelPayload.url = "https://example.com/slow";
        JSONObject cancelDetail = submitAndCancel(client, cancelPayload, "e2e-idem-cancel-1");
        check("取消任务落到 canceled",
                cancelDetail != null && "canceled".equals(cancelDetail.optString("status")));

        // 10. 网关的参数校验：kind=url 但没给 url
        try {
            JSONObject body = new JSONObject();
            body.put("kind", "url");
            client.createTask(body, "e2e-idem-badurl-1");
            check("空 url 应当被网关拒", false);
        } catch (ApiClient.ApiException exc) {
            check("空 url → HTTP 400", exc.httpStatus == 400);
            check("空 url → code=INVALID_INPUT", "INVALID_INPUT".equals(exc.code));
        }

        // 11. 查不存在的任务
        try {
            client.getTask("tsk_does_not_exist");
            check("查不存在的任务应当 404", false);
        } catch (ApiClient.ApiException exc) {
            check("查不存在的任务 → HTTP 404", exc.httpStatus == 404);
            check("查不存在的任务 → code=NOT_FOUND", "NOT_FOUND".equals(exc.code));
        }

        System.out.println();
        System.out.println("E2E 通过 " + ok + " 项，失败 " + bad + " 项");
        if (bad > 0) {
            System.exit(1);
        }
    }

    /** 提交 + 轮询到终态，返回详情。 */
    private static JSONObject submitAndWait(ApiClient client, TaskPayload payload, String idempotencyKey)
            throws Exception {
        System.out.println("--> 提交 " + payload.toJson());
        JSONObject created = client.createTask(payload.toJson(), idempotencyKey);
        String taskId = created.optString("task_id");
        check("提交返回 task_id", !taskId.isEmpty());
        check("提交返回 status", !created.optString("status").isEmpty());
        check("提交返回 created_at", !created.optString("created_at").isEmpty());
        System.out.println("    " + created);

        String status = created.optString("status", "queued");
        JSONObject detail = null;
        long deadline = System.currentTimeMillis() + 60_000;
        while (!TaskPayload.isTerminal(status) && System.currentTimeMillis() < deadline) {
            Thread.sleep(500);
            detail = client.getTask(taskId);
            status = detail.optString("status", status);
        }
        System.out.println("    终态 " + status + "  " + (detail == null ? "" : detail));
        return detail;
    }

    /** 提交后立刻取消。 */
    private static JSONObject submitAndCancel(ApiClient client, TaskPayload payload, String idempotencyKey)
            throws Exception {
        JSONObject created = client.createTask(payload.toJson(), idempotencyKey);
        String taskId = created.optString("task_id");
        check("取消流程拿到 task_id", !taskId.isEmpty());
        JSONObject canceled = client.cancelTask(taskId);
        System.out.println("    cancel 返回 " + canceled);
        return client.getTask(taskId);
    }

    private static void check(String name, boolean condition) {
        if (condition) {
            ok++;
            System.out.println("  [ok]   " + name);
        } else {
            bad++;
            System.out.println("  [FAIL] " + name);
        }
    }

    private static String first(String text, int max) {
        if (text == null) {
            return "";
        }
        return text.length() <= max ? text : text.substring(0, max) + "…";
    }

    private AppE2E() {
    }
}
