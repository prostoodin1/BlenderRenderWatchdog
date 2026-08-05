package io.github.prostoodin1.blenderwatchdog;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String PREFS = "watchdog_mobile";
    private static final String DEVICES_KEY = "devices";
    private static final String REFRESH_KEY = "refresh_ms";
    private static final int WHITE = Color.rgb(244, 249, 250);
    private static final int MUTED = Color.rgb(161, 180, 186);
    private static final int ACCENT = Color.rgb(97, 220, 203);
    private static final int DANGER = Color.rgb(255, 128, 144);

    private final ExecutorService network = Executors.newFixedThreadPool(4);
    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<Device> devices = new ArrayList<>();
    private final Map<String, DeviceBinding> deviceBindings = new LinkedHashMap<>();
    private FrameLayout content;
    private LinearLayout nav;
    private SharedPreferences preferences;
    private int currentTab = 0;
    private int refreshMs = 5000;

    private final Runnable poller = new Runnable() {
        @Override public void run() {
            if (currentTab == 0) {
                refreshDeviceStates();
                main.postDelayed(this, refreshMs);
            }
        }
    };

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(10, 17, 23));
        getWindow().setNavigationBarColor(Color.rgb(10, 17, 23));
        preferences = getSharedPreferences(PREFS, MODE_PRIVATE);
        refreshMs = preferences.getInt(REFRESH_KEY, 5000);
        loadDevices();
        buildShell();
        showDevices();
    }

    @Override protected void onDestroy() {
        main.removeCallbacks(poller);
        network.shutdownNow();
        super.onDestroy();
    }

    private void buildShell() {
        LinearLayout root = vertical();
        GradientDrawable background = new GradientDrawable(
            GradientDrawable.Orientation.TL_BR,
            new int[]{Color.rgb(20, 53, 57), Color.rgb(8, 16, 22), Color.rgb(24, 29, 43)}
        );
        root.setBackground(background);
        content = new FrameLayout(this);
        root.addView(content, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setGravity(Gravity.CENTER);
        nav.setPadding(dp(9), dp(11), dp(9), dp(8));
        nav.setLayerType(View.LAYER_TYPE_SOFTWARE, null);
        nav.setBackground(new CloudNavDrawable());
        addNavButton(R.string.devices, 0);
        addNavButton(R.string.history, 1);
        addNavButton(R.string.settings, 2);
        LinearLayout.LayoutParams navParams = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(82));
        navParams.setMargins(dp(14), 0, dp(14), dp(12));
        root.addView(nav, navParams);
        setContentView(root);
    }

    private void addNavButton(int label, int tab) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(getString(label));
        button.setTextSize(12);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setTextColor(MUTED);
        button.setGravity(Gravity.CENTER);
        button.setPadding(dp(6), 0, dp(6), 0);
        button.setBackgroundColor(Color.TRANSPARENT);
        button.setTag(tab);
        button.setOnClickListener(view -> selectTab((int) view.getTag()));
        nav.addView(button, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f));
    }

    private void selectTab(int tab) {
        currentTab = tab;
        main.removeCallbacks(poller);
        selectNavOnly(tab);
        if (tab == 0) showDevices();
        else if (tab == 1) showHistory();
        else showSettings();
    }

    private LinearLayout page(int title, int hint) {
        LinearLayout page = vertical();
        page.setPadding(dp(18), dp(20), dp(18), dp(22));
        TextView eyebrow = text(getString(R.string.eyebrow), 12, ACCENT, Typeface.BOLD);
        eyebrow.setLetterSpacing(.12f);
        page.addView(eyebrow);
        TextView heading = text(getString(title), 31, WHITE, Typeface.BOLD);
        page.addView(heading, margins(-1, -2, 0, 0, 0));
        TextView subtitle = text(getString(hint), 14, MUTED, Typeface.NORMAL);
        subtitle.setLineSpacing(0, 1.18f);
        page.addView(subtitle, margins(-1, -2, 0, dp(7), dp(14)));
        return page;
    }

    private void setPage(LinearLayout page) {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        scroll.addView(page);
        content.removeAllViews();
        content.addView(scroll, new FrameLayout.LayoutParams(-1, -1));
    }

    private void showDevices() {
        currentTab = 0;
        deviceBindings.clear();
        LinearLayout page = page(R.string.your_devices, R.string.devices_hint);
        page.addView(syncCard());
        if (devices.isEmpty()) {
            page.addView(messageCard(getString(R.string.no_devices)));
        } else {
            for (Device device : devices) page.addView(deviceCard(device));
        }
        setPage(page);
        selectNavOnly(0);
        refreshDeviceStates();
        main.removeCallbacks(poller);
        main.postDelayed(poller, refreshMs);
    }

    private View syncCard() {
        LinearLayout card = card();
        card.addView(text(getString(R.string.sync_code), 17, WHITE, Typeface.BOLD));
        EditText code = new EditText(this);
        code.setSingleLine(false);
        code.setMinLines(2);
        code.setTextColor(WHITE);
        code.setHintTextColor(MUTED);
        code.setHint("BRWM1-…");
        code.setTextSize(13);
        code.setPadding(dp(13), dp(10), dp(13), dp(10));
        code.setBackground(glass(Color.argb(126, 7, 18, 22), 16, Color.argb(75, 255, 255, 255)));
        card.addView(code, margins(-1, -2, 0, dp(10), dp(10)));

        LinearLayout actions = row();
        Button paste = actionButton(getString(R.string.paste), false);
        paste.setOnClickListener(view -> {
            ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            ClipData clip = clipboard.getPrimaryClip();
            if (clip != null && clip.getItemCount() > 0) code.setText(clip.getItemAt(0).coerceToText(this));
        });
        Button add = actionButton(getString(R.string.add_device), true);
        add.setOnClickListener(view -> {
            try {
                addDevice(Device.fromSyncCode(code.getText().toString()));
                showDevices();
            } catch (Exception error) {
                Toast.makeText(this, R.string.invalid_code, Toast.LENGTH_LONG).show();
            }
        });
        actions.addView(paste, new LinearLayout.LayoutParams(0, dp(48), 1f));
        actions.addView(space(dp(8), 1));
        actions.addView(add, new LinearLayout.LayoutParams(0, dp(48), 1.5f));
        card.addView(actions);
        return card;
    }

    private View deviceCard(Device device) {
        LinearLayout card = card();
        LinearLayout header = row();
        TextView name = text(device.name, 19, WHITE, Typeface.BOLD);
        TextView status = text(getString(R.string.connecting), 13, MUTED, Typeface.BOLD);
        status.setGravity(Gravity.END);
        header.addView(name, new LinearLayout.LayoutParams(0, -2, 1f));
        header.addView(status, new LinearLayout.LayoutParams(0, -2, .7f));
        card.addView(header);

        TextView project = text("—", 15, WHITE, Typeface.NORMAL);
        TextView detail = text(device.host + ":" + device.port, 13, MUTED, Typeface.NORMAL);
        card.addView(project, margins(-1, -2, 0, dp(10), 0));
        card.addView(detail, margins(-1, -2, 0, dp(3), dp(9)));

        LinearLayout progressRow = row();
        progressRow.addView(text(getString(R.string.progress), 12, MUTED, Typeface.BOLD), new LinearLayout.LayoutParams(0, -2, 1f));
        TextView percent = text("0%", 14, ACCENT, Typeface.BOLD);
        percent.setGravity(Gravity.END);
        progressRow.addView(percent, new LinearLayout.LayoutParams(0, -2, 1f));
        card.addView(progressRow);
        ProgressBar progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setProgressTintList(android.content.res.ColorStateList.valueOf(ACCENT));
        progress.setProgressBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(30, 50, 56)));
        card.addView(progress, margins(-1, dp(8), 0, dp(5), dp(12)));

        LinearLayout actions = row();
        Button pause = actionButton(getString(R.string.pause), true);
        pause.setOnClickListener(view -> sendAction(device, "pause"));
        Button stop = actionButton(getString(R.string.stop), false);
        stop.setTextColor(DANGER);
        stop.setOnClickListener(view -> sendAction(device, "stop"));
        Button remove = actionButton(getString(R.string.remove), false);
        remove.setOnClickListener(view -> confirmRemove(device));
        actions.addView(pause, new LinearLayout.LayoutParams(0, dp(46), 1.45f));
        actions.addView(space(dp(6), 1));
        actions.addView(stop, new LinearLayout.LayoutParams(0, dp(46), 1f));
        actions.addView(space(dp(6), 1));
        actions.addView(remove, new LinearLayout.LayoutParams(0, dp(46), 1f));
        card.addView(actions);

        deviceBindings.put(device.id(), new DeviceBinding(status, project, detail, percent, progress));
        return card;
    }

    private void refreshDeviceStates() {
        for (Device device : new ArrayList<>(devices)) {
            DeviceBinding binding = deviceBindings.get(device.id());
            if (binding == null) continue;
            network.execute(() -> {
                try {
                    JSONObject state = getJson(device, "/api/state");
                    main.post(() -> binding.online(state));
                } catch (Exception error) {
                    main.post(binding::offline);
                }
            });
        }
    }

    private void sendAction(Device device, String action) {
        network.execute(() -> {
            try {
                postJson(device, "/api/action", new JSONObject().put("action", action));
                main.post(() -> Toast.makeText(this, R.string.command_sent, Toast.LENGTH_SHORT).show());
            } catch (Exception error) {
                DeviceBinding binding = deviceBindings.get(device.id());
                if (binding != null) main.post(binding::offline);
            }
        });
    }

    private void showHistory() {
        currentTab = 1;
        LinearLayout page = page(R.string.render_history, R.string.history_hint);
        if (devices.isEmpty()) {
            page.addView(messageCard(getString(R.string.no_devices)));
        } else {
            for (Device device : devices) {
                LinearLayout card = card();
                card.addView(text(device.name, 18, WHITE, Typeface.BOLD));
                TextView loading = text(getString(R.string.loading_history), 14, MUTED, Typeface.NORMAL);
                card.addView(loading, margins(-1, -2, 0, dp(7), 0));
                page.addView(card);
                loadHistory(device, card, loading);
            }
        }
        setPage(page);
        selectNavOnly(1);
    }

    private void loadHistory(Device device, LinearLayout card, TextView loading) {
        network.execute(() -> {
            try {
                JSONArray history = getJson(device, "/api/history").optJSONArray("history");
                main.post(() -> {
                    card.removeView(loading);
                    if (history == null || history.length() == 0) {
                        card.addView(text(getString(R.string.no_history), 14, MUTED, Typeface.NORMAL));
                        return;
                    }
                    int count = Math.min(history.length(), 12);
                    for (int index = 0; index < count; index++) {
                        JSONObject record = history.optJSONObject(index);
                        if (record != null) card.addView(historyRow(record));
                    }
                });
            } catch (Exception error) {
                main.post(() -> loading.setText(R.string.offline));
            }
        });
    }

    private View historyRow(JSONObject record) {
        LinearLayout row = vertical();
        row.setPadding(dp(12), dp(10), dp(12), dp(10));
        row.setBackground(glass(Color.argb(82, 7, 18, 22), 16, Color.argb(48, 255, 255, 255)));
        String path = record.optString("project_path", "—");
        path = path.replace('\\', '/');
        String name = path.substring(path.lastIndexOf('/') + 1);
        row.addView(text(name, 15, WHITE, Typeface.BOLD));
        JSONArray metrics = record.optJSONArray("frame_metrics");
        int frames = metrics == null ? 0 : metrics.length();
        String status = record.optString("status", "unknown");
        row.addView(text(getString(R.string.completed_frames, frames, status), 12, MUTED, Typeface.NORMAL));
        return wrapMargins(row, dp(7), 0);
    }

    private void showSettings() {
        currentTab = 2;
        LinearLayout page = page(R.string.connection_settings, R.string.settings_hint);
        LinearLayout card = card();
        card.addView(text(getString(R.string.refresh_interval), 17, WHITE, Typeface.BOLD));
        LinearLayout choices = row();
        choices.addView(intervalButton(R.string.two_seconds, 2000), new LinearLayout.LayoutParams(0, dp(48), 1f));
        choices.addView(space(dp(7), 1));
        choices.addView(intervalButton(R.string.five_seconds, 5000), new LinearLayout.LayoutParams(0, dp(48), 1f));
        choices.addView(space(dp(7), 1));
        choices.addView(intervalButton(R.string.ten_seconds, 10000), new LinearLayout.LayoutParams(0, dp(48), 1f));
        card.addView(choices, margins(-1, -2, 0, dp(10), dp(14)));
        card.addView(text(getString(R.string.saved_devices, devices.size()), 14, MUTED, Typeface.NORMAL));
        Button clear = actionButton(getString(R.string.clear_devices), false);
        clear.setTextColor(DANGER);
        clear.setOnClickListener(view -> {
            devices.clear();
            saveDevices();
            showSettings();
        });
        card.addView(clear, margins(-1, dp(48), 0, dp(8), dp(12)));
        card.addView(text(getString(R.string.security_note), 13, MUTED, Typeface.NORMAL));
        page.addView(card);
        page.addView(messageCard(getString(R.string.about)));
        setPage(page);
        selectNavOnly(2);
    }

    private Button intervalButton(int text, int interval) {
        Button button = actionButton(getString(text), refreshMs == interval);
        button.setOnClickListener(view -> {
            refreshMs = interval;
            preferences.edit().putInt(REFRESH_KEY, interval).apply();
            showSettings();
        });
        return button;
    }

    private void confirmRemove(Device device) {
        new AlertDialog.Builder(this)
            .setTitle(R.string.remove_title)
            .setMessage(R.string.remove_message)
            .setNegativeButton(R.string.cancel, null)
            .setPositiveButton(R.string.remove, (dialog, which) -> {
                devices.removeIf(item -> item.id().equals(device.id()));
                saveDevices();
                showDevices();
            })
            .show();
    }

    private JSONObject getJson(Device device, String path) throws Exception {
        return request(device, path, "GET", null);
    }

    private JSONObject postJson(Device device, String path, JSONObject payload) throws Exception {
        return request(device, path, "POST", payload);
    }

    private JSONObject request(Device device, String path, String method, JSONObject payload) throws Exception {
        String token = URLEncoder.encode(device.token, StandardCharsets.UTF_8.name());
        URL url = new URL("http", device.host, device.port, path + "?token=" + token);
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(2200);
        connection.setReadTimeout(3500);
        connection.setUseCaches(false);
        if (payload != null) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.getOutputStream().write(payload.toString().getBytes(StandardCharsets.UTF_8));
        }
        int status = connection.getResponseCode();
        InputStream stream = status >= 200 && status < 300 ? connection.getInputStream() : connection.getErrorStream();
        StringBuilder body = new StringBuilder();
        if (stream != null) {
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) body.append(line);
            }
        }
        connection.disconnect();
        if (status < 200 || status >= 300) throw new IllegalStateException("HTTP " + status);
        return new JSONObject(body.toString());
    }

    private void loadDevices() {
        devices.clear();
        try {
            JSONArray stored = new JSONArray(preferences.getString(DEVICES_KEY, "[]"));
            for (int index = 0; index < stored.length(); index++) {
                JSONObject item = stored.optJSONObject(index);
                if (item != null) devices.add(Device.fromJson(item));
            }
        } catch (Exception ignored) {
            preferences.edit().remove(DEVICES_KEY).apply();
        }
    }

    private void addDevice(Device device) {
        devices.removeIf(item -> item.id().equals(device.id()));
        devices.add(0, device);
        saveDevices();
    }

    private void saveDevices() {
        JSONArray output = new JSONArray();
        for (Device device : devices) output.put(device.toJson());
        preferences.edit().putString(DEVICES_KEY, output.toString()).apply();
    }

    private LinearLayout card() {
        LinearLayout card = vertical();
        card.setPadding(dp(17), dp(16), dp(17), dp(16));
        card.setBackground(glass(Color.argb(222, 21, 40, 49), 28, Color.argb(94, 255, 255, 255)));
        card.setElevation(dp(10));
        card.setClipToOutline(true);
        card.setLayoutParams(margins(-1, -2, 0, 0, dp(13)));
        return card;
    }

    private View messageCard(String message) {
        LinearLayout card = card();
        TextView label = text(message, 14, MUTED, Typeface.NORMAL);
        label.setLineSpacing(0, 1.2f);
        card.addView(label);
        return card;
    }

    private Button actionButton(String label, boolean active) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(label);
        button.setTextSize(12);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setTextColor(active ? Color.rgb(5, 31, 29) : WHITE);
        button.setPadding(dp(7), 0, dp(7), 0);
        button.setGravity(Gravity.CENTER);
        button.setBackground(glass(
            active ? ACCENT : Color.argb(180, 33, 55, 64),
            17,
            active ? Color.argb(160, 255, 255, 255) : Color.argb(75, 255, 255, 255)
        ));
        return button;
    }

    private TextView text(String value, int size, int color, int style) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setTypeface(Typeface.create("sans", style));
        view.setIncludeFontPadding(false);
        return view;
    }

    private LinearLayout vertical() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        return layout;
    }

    private LinearLayout row() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.HORIZONTAL);
        layout.setGravity(Gravity.CENTER_VERTICAL);
        return layout;
    }

    private View space(int width, int height) {
        View view = new View(this);
        view.setLayoutParams(new LinearLayout.LayoutParams(width, height));
        return view;
    }

    private GradientDrawable glass(int color, int radius, int stroke) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radius));
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private LinearLayout.LayoutParams margins(int width, int height, float weight, int top, int bottom) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(width, height, weight);
        params.setMargins(0, top, 0, bottom);
        return params;
    }

    private View wrapMargins(View view, int top, int bottom) {
        view.setLayoutParams(margins(-1, -2, 0, top, bottom));
        return view;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void selectNavOnly(int tab) {
        for (int index = 0; index < nav.getChildCount(); index++) {
            View item = nav.getChildAt(index);
            boolean selected = index == tab;
            item.animate().alpha(selected ? 1f : .62f).scaleX(selected ? 1.03f : .96f).scaleY(selected ? 1.03f : .96f).setDuration(180).start();
            if (item instanceof Button) {
                ((Button) item).setTextColor(selected ? WHITE : MUTED);
                item.setBackground(selected
                    ? glass(Color.argb(170, 58, 104, 106), 23, Color.argb(110, 255, 255, 255))
                    : glass(Color.TRANSPARENT, 23, Color.TRANSPARENT));
            }
        }
    }

    private final class CloudNavDrawable extends Drawable {
        private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint border = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint highlight = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path cloud = new Path();

        CloudNavDrawable() {
            fill.setColor(Color.argb(238, 20, 35, 43));
            fill.setShadowLayer(dp(14), 0, dp(7), Color.argb(145, 0, 0, 0));
            border.setStyle(Paint.Style.STROKE);
            border.setStrokeWidth(dp(1));
            border.setColor(Color.argb(95, 255, 255, 255));
            highlight.setStyle(Paint.Style.STROKE);
            highlight.setStrokeWidth(dp(1));
            highlight.setColor(Color.argb(90, 255, 255, 255));
        }

        @Override public void draw(Canvas canvas) {
            RectF bounds = new RectF(getBounds());
            float inset = dp(5);
            RectF base = new RectF(bounds.left + inset, bounds.top + dp(16), bounds.right - inset, bounds.bottom - dp(5));
            cloud.reset();
            cloud.addRoundRect(base, dp(30), dp(30), Path.Direction.CW);
            for (int index = 0; index < 3; index++) {
                float center = bounds.left + bounds.width() * (index * 2 + 1) / 6f;
                Path bubble = new Path();
                bubble.addCircle(center, bounds.top + dp(24), dp(24), Path.Direction.CW);
                cloud.op(bubble, Path.Op.UNION);
            }
            canvas.drawPath(cloud, fill);
            canvas.drawPath(cloud, border);
            canvas.drawArc(base.left + dp(16), base.top + dp(3), base.right - dp(16), base.bottom - dp(19), 200, 140, false, highlight);
        }

        @Override public void setAlpha(int alpha) { fill.setAlpha(alpha); }
        @Override public void setColorFilter(ColorFilter filter) { fill.setColorFilter(filter); }
        @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    }

    private final class DeviceBinding {
        final TextView status;
        final TextView project;
        final TextView detail;
        final TextView percent;
        final ProgressBar progress;

        DeviceBinding(TextView status, TextView project, TextView detail, TextView percent, ProgressBar progress) {
            this.status = status;
            this.project = project;
            this.detail = detail;
            this.percent = percent;
            this.progress = progress;
        }

        void online(JSONObject state) {
            status.setText(R.string.online);
            status.setTextColor(ACCENT);
            project.setText(state.optString("project", "—"));
            detail.setText(state.optString("detail", state.optString("status", "")));
            int value = Math.max(0, Math.min(100, (int) Math.round(state.optDouble("progress", 0))));
            percent.setText(String.format(Locale.getDefault(), "%d%%", value));
            progress.setProgress(value, true);
        }

        void offline() {
            status.setText(R.string.offline);
            status.setTextColor(DANGER);
        }
    }

    private static final class Device {
        final String host;
        final int port;
        final String token;
        final String name;
        final String version;

        Device(String host, int port, String token, String name, String version) {
            this.host = host;
            this.port = port;
            this.token = token;
            this.name = name;
            this.version = version;
        }

        String id() { return host + ":" + port; }

        JSONObject toJson() {
            JSONObject data = new JSONObject();
            try {
                data.put("h", host).put("p", port).put("t", token).put("n", name).put("v", version);
            } catch (JSONException ignored) { }
            return data;
        }

        static Device fromJson(JSONObject data) throws JSONException {
            return new Device(data.getString("h"), data.getInt("p"), data.getString("t"), data.optString("n", data.getString("h")), data.optString("v", ""));
        }

        static Device fromSyncCode(String raw) throws Exception {
            String value = raw.trim();
            if (!value.startsWith("BRWM1-")) throw new IllegalArgumentException("prefix");
            String encoded = value.substring(6);
            while (encoded.length() % 4 != 0) encoded += "=";
            JSONObject data = new JSONObject(new String(Base64.decode(encoded, Base64.URL_SAFE | Base64.NO_WRAP), StandardCharsets.UTF_8));
            Device device = fromJson(data);
            if (device.host.isEmpty() || device.token.isEmpty() || device.port < 1 || device.port > 65535) throw new IllegalArgumentException("fields");
            return device;
        }
    }
}
