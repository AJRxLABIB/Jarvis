package com.jarvis.mobile;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.KeyguardManager;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognizerIntent;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * JARVIS Mobile: a secure remote terminal for your JARVIS PC. One screen with a bottom navigation.
 *
 * Two authentications are always kept apart, on screen and in code:
 *   - JARVIS auth (this phone's key proved itself to the PC).
 *   - Windows session (locked / active) - read from the PC, never faked. The app never sees, stores or types a
 *     Windows password, and never claims Windows is unlocked when it isn't.
 */
public class MainActivity extends Activity {
    static final String APP_VERSION = "1.0.0";
    private static final int REQ_VOICE = 1, REQ_AUTH = 2;

    private Store store;
    private Actions actions;
    private final Handler ui = new Handler(Looper.getMainLooper());

    private LinearLayout content;      // the current screen
    private LinearLayout nav;
    private String screen = "home";
    private boolean away = false;

    private Runnable pendingAfterAuth;  // retry queued while the device credential prompt is up
    private long lastMessageId = 0;
    private final List<Map<String, Object>> chatLog = new ArrayList<Map<String, Object>>();
    private Map<String, Object> lastStatus = new LinkedHashMap<String, Object>();
    private Map<String, Object> pendingApproval;
    private boolean wakeFetchTried = false;
    private int phoneTicks = 0;

    private final Runnable poller = new Runnable() {
        @Override public void run() {
            pollStatus();
            ui.postDelayed(this, 2500);
        }
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        store = new Store(this);
        actions = new Actions(store);
        actions.setAuthHandler((retry, giveUp) -> promptDeviceAuth(retry));
        buildFrame();
        handleIntent(getIntent());
        render();
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
    }

    @Override protected void onResume() {
        super.onResume();
        ui.post(poller);
    }

    @Override protected void onPause() {
        super.onPause();
        ui.removeCallbacks(poller);
    }

    // ------------------------------------------------------------------ deep links (pairing / wake setup)

    private void handleIntent(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null) return;
        if (Pairing.isPairLink(data)) {
            toast("Pairing...");
            actions.runOnPool(() -> {
                final Pairing.Outcome outcome = Pairing.pair(store, data, APP_VERSION);
                actions.onMain(() -> {
                    new AlertDialog.Builder(this).setTitle(outcome.ok ? "Paired" : "Pairing")
                            .setMessage(outcome.message).setPositiveButton("OK", null).show();
                    screen = "home";
                    render();
                });
            });
        } else if ("jarviswake".equals(data.getScheme()) && "setup".equals(data.getHost())) {
            applyWakeSetup(data);
        }
    }

    private void applyWakeSetup(Uri uri) {
        Pc pc = store.current();
        if (pc == null) {
            toast("Pair this PC first, then set up wake.");
            return;
        }
        if (uri.getQueryParameter("mac") != null) pc.mac = uri.getQueryParameter("mac");
        if (uri.getQueryParameter("broadcast") != null) pc.broadcast = uri.getQueryParameter("broadcast");
        if (uri.getQueryParameter("public") != null) pc.publicAddress = uri.getQueryParameter("public");
        if (uri.getQueryParameter("public_port") != null)
            pc.publicPort = safeInt(uri.getQueryParameter("public_port"), 9);
        if (uri.getQueryParameter("away") != null) pc.awayHost = uri.getQueryParameter("away");
        store.upsert(pc);
        toast("Wake settings saved for " + pc.name);
        render();
    }

    // ------------------------------------------------------------------ frame + navigation

    private void buildFrame() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Ui.BG);

        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        int p = Ui.dp(this, 16);
        header.setPadding(p, p, p, Ui.dp(this, 10));
        TextView title = Ui.text(this, "JARVIS", 20, Ui.CYAN);
        title.setLetterSpacing(0.3f);
        header.addView(title);
        root.addView(header);

        ScrollView scroll = new ScrollView(this);
        scroll.setLayoutParams(new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(p, 0, p, p);
        scroll.addView(content);
        root.addView(scroll);

        nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setBackgroundColor(Ui.PANEL);
        String[] tabs = {"home", "chat", "devices", "control", "missions", "settings"};
        String[] labels = {"Home", "Chat", "Devices", "Control", "Tasks", "More"};
        for (int i = 0; i < tabs.length; i++) {
            final String tab = tabs[i];
            TextView t = Ui.text(this, labels[i], 12, screen.equals(tab) ? Ui.CYAN : Ui.DIM);
            t.setGravity(Gravity.CENTER);
            t.setPadding(0, Ui.dp(this, 12), 0, Ui.dp(this, 12));
            t.setLayoutParams(new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
            t.setOnClickListener(v -> { screen = tab; render(); });
            nav.addView(t);
        }
        root.addView(nav);
        setContentView(root);
    }

    private void render() {
        for (int i = 0; i < nav.getChildCount(); i++) {
            ((TextView) nav.getChildAt(i)).setTextColor(Ui.DIM);
        }
        String[] order = {"home", "chat", "devices", "control", "missions", "settings"};
        for (int i = 0; i < order.length; i++) {
            if (order[i].equals(screen)) ((TextView) nav.getChildAt(i)).setTextColor(Ui.CYAN);
        }
        content.removeAllViews();
        if (store.current() == null && !screen.equals("devices")) {
            screen = "devices";
        }
        switch (screen) {
            case "chat": renderChat(); break;
            case "devices": renderDevices(); break;
            case "control": renderControl(); break;
            case "missions": renderMissions(); break;
            case "settings": renderSettings(); break;
            default: renderHome();
        }
    }

    // ------------------------------------------------------------------ HOME

    private void renderHome() {
        Pc pc = store.current();
        LinearLayout card = Ui.card(this);
        card.addView(Ui.text(this, "Good " + partOfDay() + ", Sir", 20, Ui.TEXT));
        card.addView(Ui.text(this, pc.name, 16, Ui.CYAN));

        String state = Json.str(lastStatus, "state", "");
        String windows = Json.str(lastStatus, "windows", "unknown");
        boolean online = !lastStatus.isEmpty();
        card.addView(Ui.text(this, online ? "● Online" : "○ Offline / not reachable",
                14, online ? Ui.GREEN : Ui.DIM));
        card.addView(Ui.text(this, "JARVIS:  ✓ Authenticated (this phone's key)", 13, Ui.GREEN));
        card.addView(Ui.text(this, "Windows:  " + windowsLabel(windows), 13,
                "active".equals(windows) ? Ui.GREEN : Ui.GOLD));
        String explain = Json.str(lastStatus, "explain", "");
        if (!explain.isEmpty()) card.addView(Ui.text(this, explain, 12, Ui.DIM));
        content.addView(card);

        if (online) {
            LinearLayout stats = Ui.card(this);
            addStat(stats, "CPU", numeric(lastStatus, "cpu"), "%");
            addStat(stats, "RAM", numeric(lastStatus, "ram"), "%");
            addStat(stats, "Battery", numeric(lastStatus, "battery"), "%");
            if (stats.getChildCount() > 0) content.addView(stats);
        }

        Button talk = Ui.button(this, "Talk to JARVIS", Ui.CYAN);
        talk.setOnClickListener(v -> { screen = "chat"; render(); startVoice(); });
        content.addView(talk);
        Button control = Ui.button(this, "Control computer", Ui.PANEL);
        control.setOnClickListener(v -> { screen = "control"; render(); });
        content.addView(control);

        boolean canWake = pc.mac != null && !pc.mac.isEmpty();
        Button wake = Ui.button(this, canWake ? "Wake computer" : "Wake-on-LAN not set up", Ui.PANEL);
        wake.setEnabled(canWake);
        wake.setOnClickListener(v -> wakeComputer(pc));
        content.addView(wake);

        Button connect = Ui.button(this, "Connect to Windows (sign in)", Ui.PANEL);
        connect.setOnClickListener(v -> connectToWindows(pc));
        content.addView(connect);

        Button cancelTask = Ui.button(this, "Cancel what Jarvis is doing", Ui.PANEL);
        cancelTask.setContentDescription("Cancel the current request on the PC. Automations keep running.");
        cancelTask.setOnClickListener(v -> actions.run(pc, away, "cancel", new LinkedHashMap<String, Object>(),
                (result, error) -> toast(error != null ? "Couldn't reach the PC."
                        : Json.str(result.json(), "message", "Cancelled."))));
        content.addView(cancelTask);

        Button emergency = Ui.button(this, "EMERGENCY STOP", Ui.RED);
        emergency.setOnClickListener(v -> emergencyStop(pc));
        content.addView(emergency);
    }

    private void addStat(LinearLayout box, String label, String value, String unit) {
        if (value == null) return;
        box.addView(Ui.text(this, label + "   " + value + unit, 14, Ui.TEXT));
    }

    // ------------------------------------------------------------------ CHAT

    private ScrollView chatScroll;
    private LinearLayout chatMessages;
    private EditText chatInput;

    private void renderChat() {
        chatMessages = new LinearLayout(this);
        chatMessages.setOrientation(LinearLayout.VERTICAL);
        for (Map<String, Object> m : chatLog) addBubble(m);
        content.addView(chatMessages);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        chatInput = new EditText(this);
        chatInput.setHint("Message JARVIS (Bangla or English)");
        chatInput.setTextColor(Ui.TEXT);
        chatInput.setHintTextColor(Ui.DIM);
        chatInput.setLayoutParams(new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        row.addView(chatInput);
        Button send = new Button(this);
        send.setText("Send");
        send.setOnClickListener(v -> sendChat(chatInput.getText().toString()));
        row.addView(send);
        content.addView(row);

        Button voice = Ui.button(this, "🎤  Hold to talk", Ui.PANEL);
        voice.setOnClickListener(v -> startVoice());
        content.addView(voice);
    }

    private void addBubble(Map<String, Object> m) {
        if (chatMessages == null) return;
        String role = Json.str(m, "role", "assistant");
        LinearLayout bubble = Ui.card(this);
        bubble.addView(Ui.text(this, role.equals("user") ? "YOU" : "JARVIS", 10,
                role.equals("user") ? Ui.CYAN : Ui.GOLD));
        bubble.addView(Ui.text(this, Json.str(m, "text", ""), 15, Ui.TEXT));
        chatMessages.addView(bubble);
    }

    private void sendChat(String text) {
        text = text.trim();
        if (text.isEmpty()) return;
        Pc pc = store.current();
        Map<String, Object> mine = new LinkedHashMap<String, Object>();
        mine.put("role", "user");
        mine.put("text", text);
        chatLog.add(mine);
        addBubble(mine);
        chatInput.setText("");
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("text", text);
        actions.run(pc, away, "chat", args, (result, error) -> {
            if (error != null) toast("Couldn't reach the PC.");
        });
    }

    private void startVoice() {
        try {
            Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak to JARVIS");
            startActivityForResult(intent, REQ_VOICE);
        } catch (Exception e) {
            toast("Voice input isn't available on this phone.");
        }
    }

    // ------------------------------------------------------------------ DEVICES

    private void renderDevices() {
        List<Pc> pcs = store.pcs();
        if (pcs.isEmpty()) {
            LinearLayout card = Ui.card(this);
            card.addView(Ui.text(this, "No computer yet", 18, Ui.TEXT));
            card.addView(Ui.text(this, "On the PC open JARVIS → Menu → Mobile devices → Pair a new "
                    + "phone, then scan the QR (it opens this app).", 13, Ui.DIM));
            content.addView(card);
            return;
        }
        Pc current = store.current();
        for (Pc pc : pcs) {
            LinearLayout card = Ui.card(this);
            card.addView(Ui.text(this, pc.name + (pc.id.equals(current.id) ? "  (selected)" : ""), 16,
                    pc.id.equals(current.id) ? Ui.CYAN : Ui.TEXT));
            card.addView(Ui.text(this, pc.host + ":" + pc.port, 12, Ui.DIM));
            card.addView(Ui.text(this, "Control key: " + (pc.authKeyRegistered ? "set" : "not set up"), 12,
                    pc.authKeyRegistered ? Ui.GREEN : Ui.GOLD));
            Button select = Ui.button(this, "Use this computer", Ui.PANEL);
            select.setOnClickListener(v -> { store.setCurrentId(pc.id); screen = "home"; render(); });
            card.addView(select);
            Button remove = Ui.button(this, "Remove", Ui.PANEL);
            remove.setOnClickListener(v -> confirm("Remove " + pc.name + "?",
                    () -> { store.remove(pc.id); render(); }));
            card.addView(remove);
            content.addView(card);
        }
    }

    // ------------------------------------------------------------------ CONTROL

    private void renderControl() {
        Pc pc = store.current();
        String windows = Json.str(lastStatus, "windows", "unknown");
        if (!"active".equals(windows)) {
            LinearLayout locked = Ui.card(this);
            locked.addView(Ui.text(this, "Windows: " + windowsLabel(windows), 16, Ui.GOLD));
            String explain = Json.str(lastStatus, "explain", "Sign in to Windows to control the screen.");
            locked.addView(Ui.text(this, explain, 13, Ui.DIM));
            Button options = Ui.button(this, "Show sign-in options", Ui.PANEL);
            options.setOnClickListener(v -> showAuthOptions(pc));
            locked.addView(options);
            content.addView(locked);
        }

        LinearLayout power = Ui.card(this);
        power.addView(Ui.text(this, "Power", 15, Ui.CYAN));
        power.addView(controlChips(pc, new String[]{"lock", "sleep", "restart", "shutdown"},
                new String[]{"Lock", "Sleep", "Restart", "Shut down"}, true));
        content.addView(power);

        LinearLayout media = Ui.card(this);
        media.addView(Ui.text(this, "Sound & media", 15, Ui.CYAN));
        LinearLayout mrow = chipRow();
        mrow.addView(cmdChip(pc, "Vol -", "volume", arg("action", "down")));
        mrow.addView(cmdChip(pc, "Vol +", "volume", arg("action", "up")));
        mrow.addView(cmdChip(pc, "Mute", "volume", arg("action", "mute")));
        mrow.addView(cmdChip(pc, "Play/Pause", "media", arg("action", "play_pause")));
        mrow.addView(cmdChip(pc, "Next", "media", arg("action", "next")));
        media.addView(mrow);
        content.addView(media);

        LinearLayout apps = Ui.card(this);
        apps.addView(Ui.text(this, "Apps & web", 15, Ui.CYAN));
        Button openApp = Ui.button(this, "Open an app...", Ui.PANEL);
        openApp.setOnClickListener(v -> ask("Open which app?", name -> {
            Map<String, Object> a = new LinkedHashMap<String, Object>();
            a.put("name", name);
            send(pc, "open_app", a);
        }));
        apps.addView(openApp);
        Button openSite = Ui.button(this, "Open a website...", Ui.PANEL);
        openSite.setOnClickListener(v -> ask("Open which website?", site -> {
            Map<String, Object> a = new LinkedHashMap<String, Object>();
            a.put("site", site);
            send(pc, "open_website", a);
        }));
        apps.addView(openSite);
        content.addView(apps);

        LinearLayout screenCard = Ui.card(this);
        screenCard.addView(Ui.text(this, "Screen (active session only)", 15, Ui.CYAN));
        Button snap = Ui.button(this, "See the screen", Ui.PANEL);
        snap.setOnClickListener(v -> fetchScreen(pc));
        screenCard.addView(snap);
        content.addView(screenCard);

        LinearLayout files = Ui.card(this);
        files.addView(Ui.text(this, "Files", 15, Ui.CYAN));
        Button browse = Ui.button(this, "Browse Desktop", Ui.PANEL);
        browse.setOnClickListener(v -> browseFiles(pc, "Desktop"));
        files.addView(browse);
        content.addView(files);
    }

    private LinearLayout chipRow() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        return row;
    }

    private LinearLayout controlChips(Pc pc, String[] cmds, String[] labels, boolean confirm) {
        LinearLayout row = chipRow();
        for (int i = 0; i < cmds.length; i++) {
            final String cmd = cmds[i];
            Button chip = Ui.chip(this, labels[i]);
            chip.setOnClickListener(v -> {
                if (confirm) confirm(labels[0] == null ? cmd : "Confirm: " + cmd + " " + pc.name + "?",
                        () -> send(pc, cmd, new LinkedHashMap<String, Object>()));
                else send(pc, cmd, new LinkedHashMap<String, Object>());
            });
            row.addView(chip);
        }
        return row;
    }

    private Button cmdChip(Pc pc, String label, String command, Map<String, Object> args) {
        Button chip = Ui.chip(this, label);
        chip.setOnClickListener(v -> send(pc, command, args));
        return chip;
    }

    // ------------------------------------------------------------------ MISSIONS

    private void renderMissions() {
        Pc pc = store.current();
        LinearLayout card = Ui.card(this);
        card.addView(Ui.text(this, "Background tasks", 16, Ui.CYAN));
        TextView body = Ui.text(this, "Loading...", 14, Ui.TEXT);
        card.addView(body);
        content.addView(card);
        actions.run(pc, away, "missions", new LinkedHashMap<String, Object>(), (result, error) -> {
            if (error != null || result == null) { body.setText("Couldn't reach the PC."); return; }
            body.setText(Json.str(result.json(), "message", "No missions."));
        });
    }

    // ------------------------------------------------------------------ SETTINGS

    private void renderSettings() {
        Pc pc = store.current();
        LinearLayout card = Ui.card(this);
        card.addView(Ui.text(this, "Security", 16, Ui.CYAN));
        card.addView(Ui.text(this, "Actions that change the PC are signed with a key your phone releases only "
                + "after fingerprint / face / PIN. Read-only status never asks.", 12, Ui.DIM));
        if (pc != null && !pc.authKeyRegistered) {
            Button enrol = Ui.button(this, "Finish control-key setup", Ui.CYAN);
            enrol.setOnClickListener(v -> enrolAuthKey(pc));
            card.addView(enrol);
        }
        content.addView(card);

        LinearLayout appCard = Ui.card(this);
        appCard.addView(Ui.text(this, "JARVIS Mobile " + APP_VERSION, 15, Ui.TEXT));
        Button update = Ui.button(this, "Check for updates", Ui.PANEL);
        update.setOnClickListener(v -> checkUpdate(pc));
        appCard.addView(update);
        content.addView(appCard);

        if (pc != null) renderSync(pc);
        renderPhoneLink();

        LinearLayout awayCard = Ui.card(this);
        awayCard.addView(Ui.text(this, "Connection", 15, Ui.CYAN));
        awayCard.addView(Ui.text(this, away ? "Using the away address (Tailscale)" : "Using the home network", 13,
                Ui.DIM));
        Button toggle = Ui.button(this, away ? "Use home network" : "Use away address (Tailscale)", Ui.PANEL);
        toggle.setOnClickListener(v -> { away = !away; render(); });
        awayCard.addView(toggle);
        content.addView(awayCard);
    }

    // ------------------------------------------------------------------ PC <-> phone (see PhoneActions)

    private void renderPhoneLink() {
        LinearLayout card = Ui.card(this);
        card.addView(Ui.text(this, "PC \u2194 phone", 16, Ui.CYAN));
        card.addView(Ui.text(this, "Let your PC do a few things here: flashlight, ring (find my phone), open an app or "
                + "a link, show a message. Opening apps/links works while this app is on screen (Android rule).", 12, Ui.DIM));
        boolean control = PhoneActions.controlAllowed(this);
        Button toggle = Ui.button(this, control ? "Let my PC control this phone: ON" : "Let my PC control this phone: OFF",
                control ? Ui.CYAN : Ui.PANEL);
        toggle.setOnClickListener(v -> { PhoneActions.setControlAllowed(this, !PhoneActions.controlAllowed(this)); render(); });
        card.addView(toggle);
        boolean forward = PhoneActions.forwardNotifications(this);
        Button notes = Ui.button(this, forward ? "Forward notifications to my PC: ON" : "Forward notifications to my PC: OFF",
                forward ? Ui.CYAN : Ui.PANEL);
        notes.setOnClickListener(v -> {
            PhoneActions.setForwardNotifications(this, !PhoneActions.forwardNotifications(this));
            render();
        });
        card.addView(notes);
        Button access = Ui.button(this, "Give notification access (Android settings)", Ui.PANEL);
        access.setOnClickListener(v -> {
            try { startActivity(new Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS")); }
            catch (Exception e) { toast("Open Settings > Notifications > Notification access."); }
        });
        card.addView(access);
        card.addView(Ui.text(this, "Codes that look like one-time passwords are removed before anything is sent. "
                + "Share > JARVIS Mobile sends text or a link to the PC.", 12, Ui.DIM));
        content.addView(card);
    }

    // ------------------------------------------------------------------ to-do sync (see SyncClient)

    private void renderSync(Pc pc) {
        SyncClient client = AppSync.client(this, pc, away);
        LinearLayout card = Ui.card(this);
        card.addView(Ui.text(this, "To-do sync", 16, Ui.CYAN));
        card.addView(Ui.text(this, "Only if you switched it on for this phone on the PC (Jarvis > mobile_sync). "
                + "Edits wait here until you sync; they are signed with your fingerprint / PIN key.", 12, Ui.DIM));
        List<Map<String, Object>> items = client.list();
        int shown = 0;
        for (Map<String, Object> item : items) {
            if (shown++ >= 12) break;
            final long id = numericLong(item, "id");
            @SuppressWarnings("unchecked") Map<String, Object> fields = (Map<String, Object>) item.get("fields");
            final boolean done = fields != null && Boolean.TRUE.equals(fields.get("done"));
            String text = fields == null ? "" : String.valueOf(fields.get("text"));
            Button row = Ui.button(this, (done ? "[x] " : "[ ] ") + text, Ui.PANEL);
            row.setOnClickListener(v -> { client.setDone(id, !done); render(); });
            card.addView(row);
        }
        if (items.isEmpty()) card.addView(Ui.text(this, "Nothing here yet. Sync to fetch your PC's to-dos.", 13, Ui.DIM));
        if (client.queued() > 0) card.addView(Ui.text(this, client.queued() + " edit(s) waiting to be sent.", 12, Ui.GOLD));
        Button add = Ui.button(this, "Add a to-do", Ui.PANEL);
        add.setOnClickListener(v -> ask("New to-do", text -> { client.addTodo(text); render(); }));
        card.addView(add);
        Button syncNow = Ui.button(this, "Sync now", Ui.CYAN);
        syncNow.setOnClickListener(v -> syncNow(pc));
        card.addView(syncNow);
        Button background = Ui.button(this, AppSync.backgroundOn(this) ? "Background checks: on (tap to turn off)"
                : "Background checks: off (tap to turn on)", Ui.PANEL);
        background.setOnClickListener(v -> {
            boolean turningOn = !AppSync.backgroundOn(this);
            if (turningOn && android.os.Build.VERSION.SDK_INT >= 33
                    && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 3);   // Android 13+: asked once, here
            }
            AppSync.setBackground(this, turningOn);
            render();
        });
        card.addView(background);
        content.addView(card);
    }

    private void syncNow(Pc pc) {
        final SyncClient client = AppSync.client(this, pc, away);
        actions.runOnPool(() -> {
            try {
                final SyncClient.Summary summary = client.sync();
                actions.onMain(() -> { toast(summary.text()); render(); });
            } catch (final Exception e) {
                if (SyncClient.isAuthError(e)) actions.onMain(() -> promptDeviceAuth(() -> syncNow(pc)));
                else actions.onMain(() -> toast("Sync failed: " + (e.getMessage() == null ? "can't reach the PC" : e.getMessage())));
            }
        });
    }

    // ------------------------------------------------------------------ actions

    private void send(Pc pc, String command, Map<String, Object> args) {
        actions.run(pc, away, command, args, (result, error) -> {
            if (error != null) { toast("Couldn't reach the PC."); return; }
            Map<String, Object> json = result.json();
            if (result.ok()) toast(Json.str(json, "message", "Done."));
            else toast(Json.str(json, "message", "Refused: " + Json.str(json, "error", "" + result.status)));
        });
    }

    private void wakeComputer(Pc pc) {
        toast("Wake request sent. Waiting for the computer...");
        actions.runOnPool(() -> {
            try {
                Wol.send(Wol.parseMac(pc.mac), pc.wakeTargets(), pc.publicPort > 0 ? pc.publicPort : 9);
            } catch (Exception e) {
                actions.onMain(() -> toast("Couldn't send the wake packet."));
                return;
            }
            boolean up = false;
            for (int i = 0; i < 30 && !up; i++) {
                up = Wol.answers(pc.host, pc.port, 2000);
                if (!up) try { Thread.sleep(3000); } catch (InterruptedException ignored) {}
            }
            final boolean reachable = up;
            actions.onMain(() -> toast(reachable ? "Computer detected. It's coming up."
                    : "No answer yet. It may still be booting, off, or unreachable from here."));
        });
    }

    /** The supported way to sign in to Windows from the phone after it is awake. JARVIS never handles the
     * Windows password or the lock screen itself - Windows (Remote Desktop) or Chrome Remote Desktop takes the
     * password in its own client. This just checks what the PC supports and opens the right app. */
    private void connectToWindows(Pc pc) {
        actions.run(pc, away, "auth_options", new java.util.LinkedHashMap<String, Object>(), (result, error) -> {
            boolean rdp = false;
            if (error == null && result != null) {
                Object a = result.json().get("auth");
                if (a instanceof Map) rdp = "available".equals(Json.str((Map<String, Object>) a, "remote_desktop", ""));
            }
            String host = pc.host;
            final boolean hasRdp = rdp;
            new AlertDialog.Builder(this)
                    .setTitle("Sign in to Windows")
                    .setMessage((hasRdp
                            ? "Remote Desktop is available on your PC. Connect with Microsoft's Windows App to \""
                              + host + "\" and sign in with your Windows account - Windows takes the password, not "
                              + "JARVIS."
                            : "Your Windows edition can't host Remote Desktop (it needs Pro/Enterprise). Use Chrome "
                              + "Remote Desktop instead: it shows the lock screen and lets you sign in from the phone. "
                              + "Set it up once at remotedesktop.google.com on the PC.")
                            + "\n\nJARVIS never sees, stores or types your Windows password, and can't unlock the "
                            + "screen itself.")
                    .setPositiveButton(hasRdp ? "Open Remote Desktop" : "Get Chrome Remote Desktop",
                            (d, w) -> { if (hasRdp) openApp("com.microsoft.rdc.androidx");
                                        else openApp("com.google.chromeremotedesktop"); })
                    .setNegativeButton("Close", null).show();
        });
    }

    /** Open an installed app, or its Play Store page if it isn't installed. */
    private void openApp(String pkg) {
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage(pkg);
            if (launch != null) { startActivity(launch); return; }
        } catch (Exception ignored) {
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=" + pkg)));
        } catch (Exception e) {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW,
                        Uri.parse("https://play.google.com/store/apps/details?id=" + pkg)));
            } catch (Exception ignored) {
                toast("Couldn't open the app store.");
            }
        }
    }

    private void emergencyStop(Pc pc) {
        confirm("Emergency stop on " + pc.name + "?", () ->
                actions.run(pc, away, "emergency_stop", new LinkedHashMap<String, Object>(),
                        (result, error) -> toast(error != null ? "Couldn't reach the PC."
                                : Json.str(result.json(), "message", "Emergency stop sent."))));
    }

    private void enrolAuthKey(Pc pc) {
        actions.runOnPool(() -> {
            try {
                if (!Crypto.ensureAuthKey()) {
                    actions.onMain(() -> toast("Set a screen lock (fingerprint / PIN) on this phone first."));
                    return;
                }
                Api.Result reg = Api.command(pc, away, pc.id, "register_auth_key", Protocol.registerAuthArgs(pc.id));
                actions.onMain(() -> {
                    if (reg.ok()) { pc.authKeyRegistered = true; store.upsert(pc); toast("Control key set."); render(); }
                    else toast(Json.str(reg.json(), "message", "Couldn't set the control key."));
                });
            } catch (Exception e) {
                actions.onMain(() -> toast("Setup failed: " + e.getMessage()));
            }
        });
    }

    private void showAuthOptions(Pc pc) {
        actions.run(pc, away, "auth_options", new LinkedHashMap<String, Object>(), (result, error) -> {
            if (error != null || result == null) { toast("Couldn't reach the PC."); return; }
            new AlertDialog.Builder(this).setTitle("Windows sign-in")
                    .setMessage(authOptionsText(result.json())).setPositiveButton("OK", null).show();
        });
    }

    @SuppressWarnings("unchecked")
    private String authOptionsText(Map<String, Object> json) {
        StringBuilder b = new StringBuilder();
        Object auth = json.get("auth");
        if (auth instanceof Map) {
            Map<String, Object> a = (Map<String, Object>) auth;
            b.append(Json.str(a, "windows", "")).append("\n\n");
            Object options = a.get("sign_in_options");
            if (options instanceof List) {
                for (Object o : (List<Object>) options) {
                    if (o instanceof Map) {
                        Map<String, Object> opt = (Map<String, Object>) o;
                        b.append("• ").append(Json.str(opt, "method", "").replace('_', ' ')).append(": ")
                                .append(String.valueOf(opt.get("detail"))).append("\n\n");
                    }
                }
            }
        }
        b.append("JARVIS never types your Windows password and cannot bypass the lock screen.");
        return b.toString();
    }

    private void fetchScreen(Pc pc) {
        toast("Capturing...");
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("max_side", 1280);
        actions.run(pc, away, "screen", args, (result, error) -> {
            if (error != null || result == null) { toast("Couldn't reach the PC."); return; }
            Map<String, Object> json = result.json();
            if (!result.ok()) { toast(Json.str(json, "message", "Screen unavailable.")); return; }
            showImage(Json.str(json, "image", ""), pc);
        });
    }

    private void showImage(String base64, Pc pc) {
        try {
            byte[] bytes = android.util.Base64.decode(base64, android.util.Base64.DEFAULT);
            final android.graphics.Bitmap bmp = android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
            final android.widget.ImageView image = new android.widget.ImageView(this);
            image.setImageBitmap(bmp);
            image.setAdjustViewBounds(true);
            image.setOnTouchListener((v, event) -> {
                if (event.getAction() == android.view.MotionEvent.ACTION_UP && bmp != null) {
                    float fx = Math.max(0, Math.min(1, event.getX() / v.getWidth()));
                    float fy = Math.max(0, Math.min(1, event.getY() / v.getHeight()));
                    Map<String, Object> a = new LinkedHashMap<String, Object>();
                    a.put("action", "click");
                    a.put("x", (double) fx);
                    a.put("y", (double) fy);
                    send(pc, "mouse", a);
                }
                return true;
            });
            new AlertDialog.Builder(this).setTitle("Screen (tap to click)").setView(image)
                    .setPositiveButton("Close", null).show();
        } catch (Exception e) {
            toast("Couldn't show the screen.");
        }
    }

    @SuppressWarnings("unchecked")
    private void browseFiles(Pc pc, String path) {
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("path", path);
        actions.run(pc, away, "list_files", args, (result, error) -> {
            if (error != null || result == null) { toast("Couldn't reach the PC."); return; }
            Map<String, Object> json = result.json();
            if (!result.ok()) { toast(Json.str(json, "message", "Can't open that folder.")); return; }
            Object entries = json.get("entries");
            List<String> names = new ArrayList<String>();
            if (entries instanceof List) {
                for (Object o : (List<Object>) entries) {
                    if (o instanceof Map) {
                        Map<String, Object> e = (Map<String, Object>) o;
                        names.add((Json.bool(e, "dir") ? "📁 " : "📄 ") + Json.str(e, "name", ""));
                    }
                }
            }
            new AlertDialog.Builder(this).setTitle(Json.str(json, "path", path))
                    .setItems(names.toArray(new String[0]), null).setPositiveButton("Close", null).show();
        });
    }

    private void checkUpdate(Pc pc) {
        actions.runOnPool(() -> {
            try {
                Api.Result r = Api.post(pc, away, "/api/mobile/app-info", "{}");
                Map<String, Object> json = r.json();
                final String latest = Json.str(json, "version", APP_VERSION);
                final String sha = Json.str(json, "sha256", "");
                actions.onMain(() -> {
                    if (!latest.equals(APP_VERSION)) {
                        new AlertDialog.Builder(this).setTitle("Update available")
                                .setMessage("This PC has JARVIS Mobile " + latest + " (you have " + APP_VERSION
                                        + ").\nSHA-256:\n" + sha + "\n\nOpen the PC's Mobile devices page to install it.")
                                .setPositiveButton("OK", null).show();
                    } else {
                        toast("You're on the latest version (" + APP_VERSION + ").");
                    }
                });
            } catch (Exception e) {
                actions.onMain(() -> toast("Couldn't check with the PC."));
            }
        });
    }

    // ------------------------------------------------------------------ polling

    private void pollStatus() {
        final Pc pc = store.current();
        if (pc == null) return;
        if (!wakeFetchTried && (pc.mac == null || pc.mac.isEmpty())) {
            wakeFetchTried = true;  // one attempt per app run
            actions.runOnPool(() -> {
                Pairing.fetchWakeConfig(store, pc);
                actions.onMain(() -> { if (screen.equals("home")) render(); });
            });
        }
        actions.run(pc, away, "status", new LinkedHashMap<String, Object>(), (result, error) -> {
            if (error == null && result != null && result.status < 400) {
                Object status = result.json().get("status");
                if (status instanceof Map) {
                    lastStatus = (Map<String, Object>) status;
                    if (screen.equals("home") || screen.equals("control")) render();
                }
            } else if (screen.equals("home")) {
                lastStatus = new LinkedHashMap<String, Object>();
            }
        });
        pollEvents(pc);
        if (PhoneActions.controlAllowed(this) || ++phoneTicks % 12 == 0) {   // actions from the PC; state now and then
            final boolean awayNow = away;
            actions.runOnPool(() -> {
                try { PhoneActions.checkIn(MainActivity.this, pc, awayNow); } catch (Exception ignored) { /* PC offline */ }
            });
        }
    }

    @SuppressWarnings("unchecked")
    private void pollEvents(Pc pc) {
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("since", lastMessageId);
        actions.run(pc, away, "events", args, (result, error) -> {
            if (error != null || result == null || result.status >= 400) return;
            Map<String, Object> json = result.json();
            Object messages = json.get("messages");
            if (messages instanceof List) {
                for (Object o : (List<Object>) messages) {
                    if (o instanceof Map) {
                        Map<String, Object> m = (Map<String, Object>) o;
                        long id = (long) numericLong(m, "id");
                        if (id > lastMessageId) lastMessageId = id;
                        if ("assistant".equals(Json.str(m, "role", "")) || "notice".equals(Json.str(m, "role", ""))
                                || "reminder".equals(Json.str(m, "role", ""))) {
                            chatLog.add(m);
                            if (screen.equals("chat")) addBubble(m);
                        }
                    }
                }
            }
            Object pending = json.get("pending");
            if (pending instanceof Map) showApproval((Map<String, Object>) pending, pc);
        });
    }

    private void showApproval(Map<String, Object> pending, Pc pc) {
        String id = Json.str(pending, "id", "");
        if (id.isEmpty() || (pendingApproval != null && id.equals(Json.str(pendingApproval, "id", "")))) return;
        pendingApproval = pending;
        new AlertDialog.Builder(this).setTitle("JARVIS needs approval")
                .setMessage(Json.str(pending, "text", ""))
                .setPositiveButton("Approve", (d, w) -> answerApproval(pc, id, true))
                .setNegativeButton("Deny", (d, w) -> answerApproval(pc, id, false))
                .setOnDismissListener(d -> pendingApproval = null).show();
    }

    private void answerApproval(Pc pc, String id, boolean approved) {
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("id", id);
        args.put("approved", approved);
        actions.run(pc, away, "approve", args, (result, error) -> pendingApproval = null);
    }

    // ------------------------------------------------------------------ device auth (step-up)

    private void promptDeviceAuth(Runnable retry) {
        KeyguardManager km = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
        Intent intent = km == null ? null : km.createConfirmDeviceCredentialIntent("JARVIS",
                "Confirm it's you to control the PC");
        if (intent == null) {
            toast("Set a screen lock on this phone to control the PC.");
            return;
        }
        pendingAfterAuth = retry;
        startActivityForResult(intent, REQ_AUTH);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_VOICE && resultCode == Activity.RESULT_OK && data != null) {
            ArrayList<String> said = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);
            if (said != null && !said.isEmpty()) { screen = "chat"; render(); sendChat(said.get(0)); }
        } else if (requestCode == REQ_AUTH) {
            Runnable retry = pendingAfterAuth;
            pendingAfterAuth = null;
            if (resultCode == Activity.RESULT_OK && retry != null) retry.run();
            else toast("Not confirmed - the action was not sent.");
        }
    }

    // ------------------------------------------------------------------ small helpers

    private interface Answer { void got(String value); }

    private void ask(String title, Answer answer) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        new AlertDialog.Builder(this).setTitle(title).setView(input)
                .setPositiveButton("OK", (d, w) -> {
                    String v = input.getText().toString().trim();
                    if (!v.isEmpty()) answer.got(v);
                }).setNegativeButton("Cancel", null).show();
    }

    private void confirm(String message, Runnable onYes) {
        new AlertDialog.Builder(this).setTitle("Confirm").setMessage(message)
                .setPositiveButton("Yes", (d, w) -> onYes.run()).setNegativeButton("No", null).show();
    }

    private Map<String, Object> arg(String key, Object value) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put(key, value);
        return m;
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    private String windowsLabel(String windows) {
        if ("active".equals(windows)) return "✓ Session active";
        if ("locked".equals(windows)) return "🔒 Locked";
        return "Unknown";
    }

    private String partOfDay() {
        int hour = java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY);
        return hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
    }

    private String numeric(Map<String, Object> map, String key) {
        Object v = map.get(key);
        if (v == null) return null;
        if (v instanceof Number) return String.valueOf(((Number) v).intValue());
        return v.toString();
    }

    private long numericLong(Map<String, Object> map, String key) {
        Object v = map.get(key);
        return v instanceof Number ? ((Number) v).longValue() : 0L;
    }

    private int safeInt(String s, int fallback) {
        try { return Integer.parseInt(s); } catch (Exception e) { return fallback; }
    }
}
