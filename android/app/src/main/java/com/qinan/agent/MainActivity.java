package com.qinan.agent;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.RadioButton;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONException;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 一个极简的测试端：填地址与内容 → 点「发送任务」→ 轮询到终态 → 看结果。
 *
 * 刻意不用 AndroidX / Material，只用系统控件，好处是依赖为零、APK 极小、
 * 也不需要 Android Studio 就能构建（见 android/README.md）。
 */
public class MainActivity extends Activity {

    private static final String PREFS = "qinan-agent";
    private static final int POLL_FAST_MS = 2000;
    private static final int POLL_SLOW_MS = 5000;
    private static final int POLL_FAST_WINDOW_MS = 30000;
    private static final int POLL_MAX_MS = 300000;

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final SimpleDateFormat clock = new SimpleDateFormat("HH:mm:ss", Locale.US);

    private volatile boolean stopRequested;
    private volatile String currentTaskId;

    private EditText etBaseUrl;
    private CheckBox cbTrustAll;
    private RadioButton rbAuthPassword;
    private RadioButton rbAuthToken;
    private EditText etUsername;
    private EditText etPassword;
    private EditText etToken;
    private Button btnLogin;
    private RadioButton rbKindUrl;
    private RadioButton rbKindText;
    private EditText etUrl;
    private EditText etText;
    private EditText etInstruction;
    private EditText etMaxOutput;
    private EditText etCallbackUrl;
    private EditText etClientTaskId;
    private EditText etIdempotencyKey;
    private CheckBox cbRawJson;
    private EditText etRawJson;
    private Button btnSend;
    private Button btnCancel;
    private Button btnCopy;
    private ProgressBar progress;
    private TextView tvStatus;
    private TextView tvResult;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        bindViews();
        loadPrefs();
        wireActions();
    }

    @Override
    protected void onPause() {
        super.onPause();
        savePrefs();
    }

    @Override
    protected void onDestroy() {
        stopRequested = true;
        worker.shutdownNow();
        super.onDestroy();
    }

    // ---------- 界面 ----------

    private void bindViews() {
        etBaseUrl = findViewById(R.id.etBaseUrl);
        cbTrustAll = findViewById(R.id.cbTrustAll);
        rbAuthPassword = findViewById(R.id.rbAuthPassword);
        rbAuthToken = findViewById(R.id.rbAuthToken);
        etUsername = findViewById(R.id.etUsername);
        etPassword = findViewById(R.id.etPassword);
        etToken = findViewById(R.id.etToken);
        btnLogin = findViewById(R.id.btnLogin);
        rbKindUrl = findViewById(R.id.rbKindUrl);
        rbKindText = findViewById(R.id.rbKindText);
        etUrl = findViewById(R.id.etUrl);
        etText = findViewById(R.id.etText);
        etInstruction = findViewById(R.id.etInstruction);
        etMaxOutput = findViewById(R.id.etMaxOutput);
        etCallbackUrl = findViewById(R.id.etCallbackUrl);
        etClientTaskId = findViewById(R.id.etClientTaskId);
        etIdempotencyKey = findViewById(R.id.etIdempotencyKey);
        cbRawJson = findViewById(R.id.cbRawJson);
        etRawJson = findViewById(R.id.etRawJson);
        btnSend = findViewById(R.id.btnSend);
        btnCancel = findViewById(R.id.btnCancel);
        btnCopy = findViewById(R.id.btnCopyResult);
        progress = findViewById(R.id.progress);
        tvStatus = findViewById(R.id.tvStatus);
        tvResult = findViewById(R.id.tvResult);
    }

    private void wireActions() {
        btnLogin.setOnClickListener(view -> doLogin());
        btnSend.setOnClickListener(view -> doSend());
        btnCancel.setOnClickListener(view -> doCancel());
        btnCopy.setOnClickListener(view -> copyResult());
        rbKindUrl.setOnCheckedChangeListener((button, checked) -> syncKindHint());
        rbKindText.setOnCheckedChangeListener((button, checked) -> syncKindHint());
        cbRawJson.setOnCheckedChangeListener((button, checked) -> {
            etRawJson.setVisibility(checked ? android.view.View.VISIBLE : android.view.View.GONE);
            if (checked && etRawJson.getText().toString().trim().isEmpty()) {
                etRawJson.setText(previewJson());
            }
        });
        findViewById(R.id.btnPreview).setOnClickListener(view -> {
            etRawJson.setText(previewJson());
            cbRawJson.setChecked(true);
        });
    }

    private void syncKindHint() {
        boolean url = rbKindUrl.isChecked();
        etUrl.setEnabled(url);
        etText.setEnabled(!url);
    }

    private void log(String line) {
        ui.post(() -> tvStatus.append(clock.format(new Date()) + "  " + line + "\n"));
    }

    private void showToast(String text) {
        ui.post(() -> Toast.makeText(this, text, Toast.LENGTH_SHORT).show());
    }

    private void setBusy(boolean busy) {
        ui.post(() -> {
            progress.setVisibility(busy ? android.view.View.VISIBLE : android.view.View.GONE);
            btnSend.setEnabled(!busy);
            btnCancel.setEnabled(busy);
            btnLogin.setEnabled(!busy);
        });
    }

    // ---------- 发请求 ----------

    private String baseUrl() {
        return ApiClient.stripTrailingSlash(etBaseUrl.getText().toString().trim());
    }

    private ApiClient newClient() {
        ApiClient client = new ApiClient(baseUrl(), cbTrustAll.isChecked());
        client.setToken(etToken.getText().toString().trim());
        return client;
    }

    private void doLogin() {
        String username = etUsername.getText().toString().trim();
        String password = etPassword.getText().toString();
        if (baseUrl().isEmpty()) {
            showToast("先填服务地址");
            return;
        }
        if (username.isEmpty() || password.isEmpty()) {
            showToast("先填用户名和密码");
            return;
        }
        setBusy(true);
        log("登录中…");
        worker.execute(() -> {
            try {
                JSONObject result = newClient().login(username, password, "android-app");
                String token = result.optString("access_token", "");
                JSONObject user = result.optJSONObject("user");
                ui.post(() -> etToken.setText(token));
                log("登录成功，user=" + (user == null ? "?" : user.optString("username"))
                        + "  配额=" + (user == null ? "?" : user.optInt("daily_quota")));
            } catch (Exception exc) {
                log("登录失败：" + describe(exc));
            } finally {
                setBusy(false);
            }
        });
    }

    private void doSend() {
        if (baseUrl().isEmpty()) {
            showToast("先填服务地址");
            return;
        }

        boolean rawMode = cbRawJson.isChecked();
        String rawText = etRawJson.getText().toString().trim();
        JSONObject body;
        if (rawMode) {
            try {
                body = new JSONObject(rawText);
            } catch (JSONException exc) {
                showToast("原始 JSON 不合法：" + exc.getMessage());
                return;
            }
        } else {
            TaskPayload payload = collectPayload();
            String problem = payload.validate();
            if (problem != null) {
                showToast(problem);
                return;
            }
            try {
                body = payload.toJson();
            } catch (JSONException exc) {
                showToast("组装请求体失败：" + exc.getMessage());
                return;
            }
        }

        String idempotencyKey = etIdempotencyKey.getText().toString().trim();
        if (idempotencyKey.isEmpty()) {
            idempotencyKey = UUID.randomUUID().toString();
            final String generated = idempotencyKey;
            ui.post(() -> etIdempotencyKey.setText(generated));
        }

        savePrefs();
        stopRequested = false;
        setBusy(true);
        tvStatus.setText("");
        tvResult.setText("");
        log("提交到 " + baseUrl() + "/tasks");

        final JSONObject requestBody = body;
        final String key = idempotencyKey;
        worker.execute(() -> runTask(requestBody, key));
    }

    private void runTask(JSONObject body, String idempotencyKey) {
        try {
            ApiClient client = newClient();
            if (client.getToken().isEmpty() && rbAuthPassword.isChecked()) {
                log("还没有 Token，先登录…");
                client.login(etUsername.getText().toString().trim(),
                        etPassword.getText().toString(), "android-app");
                String token = client.getToken();
                ui.post(() -> etToken.setText(token));
                log("登录成功");
            }

            JSONObject created = client.createTask(body, idempotencyKey);
            String taskId = created.optString("task_id", "");
            currentTaskId = taskId;
            log("已受理 task_id=" + taskId + "  status=" + created.optString("status")
                    + "  queue_pos=" + created.optInt("queue_pos"));

            if (taskId.isEmpty()) {
                log("响应里没有 task_id，无法轮询");
                return;
            }

            long startedAt = System.currentTimeMillis();
            String status = created.optString("status", "queued");
            JSONObject detail = null;
            while (!stopRequested && !TaskPayload.isTerminal(status)) {
                long waited = System.currentTimeMillis() - startedAt;
                if (waited > POLL_MAX_MS) {
                    log("本地等待超过 " + (POLL_MAX_MS / 1000) + " 秒，停止轮询（服务端仍在跑）");
                    break;
                }
                sleep(waited < POLL_FAST_WINDOW_MS ? POLL_FAST_MS : POLL_SLOW_MS);
                if (stopRequested) {
                    break;
                }
                try {
                    detail = client.getTask(taskId);
                } catch (ApiClient.ApiException exc) {
                    if (exc.httpStatus == 404) {
                        log("查询返回 404：" + exc.describe());
                        break;
                    }
                    log("查询失败（继续重试）：" + exc.describe());
                    continue;
                }
                String next = detail.optString("status", status);
                if (!next.equals(status)) {
                    log("状态变为 " + next);
                    status = next;
                }
            }

            if (stopRequested) {
                log("已停止本地轮询");
                return;
            }
            if (detail == null) {
                detail = client.getTask(taskId);
            }
            renderResult(detail);
        } catch (ApiClient.ApiException exc) {
            log("失败：" + exc.describe());
        } catch (Exception exc) {
            log("失败：" + describe(exc));
        } finally {
            currentTaskId = null;
            setBusy(false);
        }
    }

    private void renderResult(JSONObject detail) {
        String status = detail.optString("status", "");
        JSONObject usage = detail.optJSONObject("usage");
        JSONObject source = detail.optJSONObject("source");
        JSONObject error = detail.optJSONObject("error");

        StringBuilder summary = new StringBuilder();
        summary.append("状态：").append(status);
        if (usage != null) {
            summary.append("    tokens_in=").append(usage.optInt("tokens_in"))
                    .append(" tokens_out=").append(usage.optInt("tokens_out"))
                    .append(" 耗时=").append(usage.optInt("duration_ms")).append("ms");
        }
        if (source != null && source.has("fetched_chars") && !source.isNull("fetched_chars")) {
            summary.append("   抓到正文=").append(source.optInt("fetched_chars")).append(" 字");
        }
        if (error != null) {
            summary.append("\n错误：[").append(error.optString("code")).append("] ")
                    .append(error.optString("message"));
        }
        log(summary.toString());

        String resultMd = detail.isNull("result_md") ? "" : detail.optString("result_md", "");
        ui.post(() -> tvResult.setText(resultMd));
    }

    private void doCancel() {
        stopRequested = true;
        final String taskId = currentTaskId;
        log("已请求停止本地轮询");
        if (taskId == null || taskId.isEmpty()) {
            return;
        }
        worker.execute(() -> {
            try {
                JSONObject result = newClient().cancelTask(taskId);
                log("取消接口返回：" + result.optString("status"));
            } catch (Exception exc) {
                log("取消失败：" + describe(exc));
            }
        });
    }

    private void copyResult() {
        String text = tvResult.getText().toString();
        if (text.isEmpty()) {
            showToast("还没有结果");
            return;
        }
        ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        clipboard.setPrimaryClip(ClipData.newPlainText("result_md", text));
        showToast("已复制结果");
    }

    // ---------- 参数与持久化 ----------

    private TaskPayload collectPayload() {
        TaskPayload payload = rbKindUrl.isChecked() ? TaskPayload.forUrl() : TaskPayload.forText();
        payload.url = etUrl.getText().toString();
        payload.text = etText.getText().toString();
        payload.instruction = etInstruction.getText().toString();
        payload.maxOutputChars = etMaxOutput.getText().toString();
        payload.callbackUrl = etCallbackUrl.getText().toString();
        payload.clientTaskId = etClientTaskId.getText().toString();
        return payload;
    }

    private String previewJson() {
        TaskPayload payload = collectPayload();
        try {
            return payload.toJson().toString(2);
        } catch (JSONException exc) {
            return "{\n  // 组装失败: " + exc.getMessage() + "\n}";
        }
    }

    private static String describe(Throwable exc) {
        if (exc instanceof ApiClient.ApiException) {
            return ((ApiClient.ApiException) exc).describe();
        }
        String message = exc.getMessage();
        return exc.getClass().getSimpleName() + (message == null ? "" : ": " + message);
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException exc) {
            Thread.currentThread().interrupt();
        }
    }

    private void loadPrefs() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        etBaseUrl.setText(prefs.getString("baseUrl", "https://agent.example.com/api/v1"));
        cbTrustAll.setChecked(prefs.getBoolean("trustAll", false));
        rbAuthPassword.setChecked(prefs.getBoolean("authPassword", true));
        rbAuthToken.setChecked(!prefs.getBoolean("authPassword", true));
        etUsername.setText(prefs.getString("username", ""));
        etPassword.setText(prefs.getString("password", ""));
        etToken.setText(prefs.getString("token", ""));
        boolean kindUrl = prefs.getBoolean("kindUrl", false);
        rbKindUrl.setChecked(kindUrl);
        rbKindText.setChecked(!kindUrl);
        etUrl.setText(prefs.getString("url", ""));
        etText.setText(prefs.getString("text", ""));
        etInstruction.setText(prefs.getString("instruction", ""));
        etMaxOutput.setText(prefs.getString("maxOutputChars", "1200"));
        etCallbackUrl.setText(prefs.getString("callbackUrl", ""));
        etClientTaskId.setText(prefs.getString("clientTaskId", ""));
        syncKindHint();
    }

    private void savePrefs() {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString("baseUrl", etBaseUrl.getText().toString().trim())
                .putBoolean("trustAll", cbTrustAll.isChecked())
                .putBoolean("authPassword", rbAuthPassword.isChecked())
                .putString("username", etUsername.getText().toString().trim())
                .putString("password", etPassword.getText().toString())
                .putString("token", etToken.getText().toString().trim())
                .putBoolean("kindUrl", rbKindUrl.isChecked())
                .putString("url", etUrl.getText().toString().trim())
                .putString("text", etText.getText().toString())
                .putString("instruction", etInstruction.getText().toString().trim())
                .putString("maxOutputChars", etMaxOutput.getText().toString().trim())
                .putString("callbackUrl", etCallbackUrl.getText().toString().trim())
                .putString("clientTaskId", etClientTaskId.getText().toString().trim())
                .apply();
    }
}
