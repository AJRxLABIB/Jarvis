package com.jarvis.wake;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

/**
 * One screen: a big "Wake up my PC" button, what happened, and the settings (filled in by Jarvis's
 * "Set up the wake app" link, or by hand). Nothing is claimed that wasn't seen: after sending the magic packet
 * the app waits until the PC answers on Jarvis's port, and says so either way.
 */
public class MainActivity extends Activity {
    private static final int BG = Color.rgb(2, 5, 11), PANEL = Color.rgb(7, 18, 31), CYAN = Color.rgb(56, 189, 248),
            TEXT = Color.rgb(214, 230, 245), DIM = Color.rgb(111, 139, 166), GOLD = Color.rgb(245, 182, 66);
    private static final String[] KEYS = {"name", "mac", "broadcast", "ip", "port", "public", "public_port", "away"};
    private static final String[] LABELS = {"PC name", "MAC address (of the wired adapter)",
            "Home broadcast address(es)", "PC address at home", "Jarvis port (to check it's on)",
            "Internet address (router's public IP or DDNS name, optional)", "Forwarded UDP port on the router",
            "Away check address (Tailscale IP, optional)"};
    private static final int WAIT_SECONDS = 150;

    private SharedPreferences prefs;
    private final EditText[] fields = new EditText[KEYS.length];
    private TextView title, status;
    private Button wake;
    private volatile boolean busy;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("wake", MODE_PRIVATE);
        buildScreen();
        handleSetupLink(getIntent());
        showSaved();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleSetupLink(intent);
        showSaved();
    }

    /** jarviswake://setup?name=..&mac=..&broadcast=..&ip=..&port=..&public=..&public_port=..&away=.. */
    private void handleSetupLink(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null || !"jarviswake".equals(data.getScheme())) return;
        String mac = data.getQueryParameter("mac");
        if (Wol.parseMac(mac) == null) {
            say("That setup link has no usable MAC address. Open Jarvis on the PC: Menu -> Phone remote.", GOLD);
            return;
        }
        SharedPreferences.Editor editor = prefs.edit();
        for (String key : KEYS) {
            String value = data.getQueryParameter(key);
            if (value != null) editor.putString(key, value.trim());
        }
        editor.apply();
        say("Set up for " + prefs.getString("name", "your PC") + ". Press the button to wake it.", CYAN);
    }

    private void buildScreen() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(BG);
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(18);
        column.setPadding(pad, pad, pad, pad);
        scroll.addView(column);

        title = text("Jarvis Wake", 26, CYAN);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        column.addView(title);
        column.addView(text("Wakes your PC with Wake-on-LAN, then checks that it really came on.", 14, DIM));

        wake = new Button(this);
        wake.setText("WAKE UP MY PC");
        wake.setTextSize(20);
        wake.setTextColor(BG);
        wake.setBackgroundColor(CYAN);
        wake.setPadding(pad, dp(22), pad, dp(22));
        LinearLayout.LayoutParams big = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        big.setMargins(0, dp(22), 0, dp(12));
        wake.setLayoutParams(big);
        wake.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { startWake(); }
        });
        column.addView(wake);

        Button check = button("Is it on?");
        check.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { startCheck(); }
        });
        column.addView(check);

        status = text("", 16, TEXT);
        status.setPadding(0, dp(12), 0, dp(12));
        column.addView(status);

        Button open = button("Open Jarvis (phone remote)");
        open.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { openRemote(); }
        });
        column.addView(open);

        TextView settings = text("Settings", 18, CYAN);
        settings.setPadding(0, dp(24), 0, dp(4));
        column.addView(settings);
        column.addView(text("Filled in by Jarvis: on the PC open Menu -> Phone remote, open the page on this phone "
                + "and tap 'Set up the wake app'. You can also type them.", 13, DIM));
        for (int i = 0; i < KEYS.length; i++) {
            column.addView(text(LABELS[i], 13, DIM));
            EditText field = new EditText(this);
            field.setTextColor(TEXT);
            field.setBackgroundColor(PANEL);
            field.setPadding(dp(10), dp(8), dp(10), dp(8));
            field.setSingleLine(true);
            if (KEYS[i].endsWith("port")) field.setInputType(InputType.TYPE_CLASS_NUMBER);
            fields[i] = field;
            column.addView(field);
        }
        Button save = button("Save settings");
        save.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { saveTyped(); }
        });
        column.addView(save);
        column.addView(text("Honest limits: the PC must support Wake-on-LAN (usually wired Ethernet, from sleep; Jarvis "
                + "checks this - say 'can you wake this PC' to it). From another network the magic packet goes to "
                + "your router, which must forward the UDP port to the PC; many routers can't. The app only says the "
                + "PC is on when it really answers.", 12, DIM));
        setContentView(scroll);
    }

    private void showSaved() {
        for (int i = 0; i < KEYS.length; i++) fields[i].setText(prefs.getString(KEYS[i], ""));
        String name = prefs.getString("name", "");
        title.setText(name.isEmpty() ? "Jarvis Wake" : "Wake " + name);
        if (prefs.getString("mac", "").isEmpty()) {
            say("Not set up yet. On the PC: Jarvis -> Menu -> Phone remote, open the page on this phone and tap "
                    + "'Set up the wake app'.", GOLD);
        }
    }

    private void saveTyped() {
        if (Wol.parseMac(fields[1].getText().toString()) == null) {
            say("That MAC address doesn't look right (like 3C:52:82:1A:2B:3C).", GOLD);
            return;
        }
        SharedPreferences.Editor editor = prefs.edit();
        for (int i = 0; i < KEYS.length; i++) editor.putString(KEYS[i], fields[i].getText().toString().trim());
        editor.apply();
        showSaved();
        say("Saved.", CYAN);
    }

    private int port(String key, int fallback) {
        try { return Integer.parseInt(prefs.getString(key, "")); } catch (NumberFormatException e) { return fallback; }
    }

    private void startWake() {
        final byte[] mac = Wol.parseMac(prefs.getString("mac", ""));
        if (mac == null) { say("Set up the app first (see Settings below).", GOLD); return; }
        if (busy) return;
        busy = true;
        wake.setEnabled(false);
        say("Sending the wake-up signal...", TEXT);
        new Thread(new Runnable() {
            @Override public void run() {
                List<String> targets = new ArrayList<String>(Wol.split(prefs.getString("broadcast", "")));
                targets.add("255.255.255.255");
                String internet = prefs.getString("public", "").trim();
                if (!internet.isEmpty()) targets.add(internet + ":" + port("public_port", 9));
                try {
                    int sent = Wol.send(mac, targets, 9);
                    post("Signal sent (" + sent + " packets). Waiting for the PC to answer...", TEXT);
                } catch (Exception e) {
                    post("Couldn't send the signal: " + e.getMessage() + ". Is the phone online?", GOLD);
                    done();
                    return;
                }
                String home = prefs.getString("ip", ""), away = prefs.getString("away", "");
                int jarvis = port("port", 8765);
                for (int second = 0; second < WAIT_SECONDS; second += 3) {
                    if (Wol.answers(home, jarvis, 1500) || Wol.answers(away, jarvis, 1500)) {
                        post("Your PC is ON - Jarvis answered after about " + second + " s.", CYAN);
                        done();
                        return;
                    }
                    try { Thread.sleep(1500); } catch (InterruptedException e) { break; }
                }
                post("No answer after " + WAIT_SECONDS / 60 + " minutes. The PC may still be off: Wake-on-LAN may "
                        + "not be set up, it may be on Wi-Fi only, or (from outside) the router didn't pass the "
                        + "signal on. If Jarvis isn't set to start with Windows it can be on without answering.", GOLD);
                done();
            }
        }).start();
    }

    private void startCheck() {
        if (busy) return;
        say("Checking...", TEXT);
        new Thread(new Runnable() {
            @Override public void run() {
                int jarvis = port("port", 8765);
                boolean on = Wol.answers(prefs.getString("ip", ""), jarvis, 2000)
                        || Wol.answers(prefs.getString("away", ""), jarvis, 2000);
                post(on ? "Your PC is ON (Jarvis answered)." : "No answer: the PC is off, asleep, or not reachable "
                        + "from this network.", on ? CYAN : GOLD);
            }
        }).start();
    }

    private void openRemote() {
        String home = prefs.getString("ip", "");
        if (home.isEmpty()) { say("Set up the app first.", GOLD); return; }
        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://" + home + ":" + port("port", 8765) + "/")));
    }

    private void done() {
        runOnUiThread(new Runnable() {
            @Override public void run() { busy = false; wake.setEnabled(true); }
        });
    }

    private void post(final String message, final int color) {
        runOnUiThread(new Runnable() {
            @Override public void run() { say(message, color); }
        });
    }

    private void say(String message, int color) {
        status.setText(message);
        status.setTextColor(color);
    }

    private TextView text(String value, int size, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setPadding(0, dp(4), 0, dp(4));
        return view;
    }

    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextColor(TEXT);
        button.setBackgroundColor(PANEL);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        params.setMargins(0, dp(6), 0, dp(6));
        button.setLayoutParams(params);
        button.setGravity(Gravity.CENTER);
        return button;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
