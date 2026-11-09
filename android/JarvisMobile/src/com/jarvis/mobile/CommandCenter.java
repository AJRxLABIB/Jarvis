package com.jarvis.mobile;

import android.content.Context;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The extra "Command center" screens, kept out of MainActivity: Overview (what the PC reports: situations, security, cross-device
 * tasks, what changed), Find my PC (local discovery) and Phone sensors (what this phone may tell the PC). Everything shown here is
 * either read from the PC through the same signed read-only commands the other screens use, or comes from this phone; nothing is
 * computed or invented here, and a section the PC cannot give is shown as "unavailable".
 */
public final class CommandCenter {
    private CommandCenter() {}

    /** What the screens need from the Activity. */
    public interface Host {
        Context context();
        LinearLayout content();
        Store store();
        Actions actions();
        boolean away();
        void toast(String message);
        void show(String screen);
        void permissions(String[] permissions, int code);
        void takePhoto();
    }

    private static Discovery discovery;

    public static boolean owns(String screen) {
        return "center".equals(screen) || "center_overview".equals(screen) || "center_find".equals(screen) || "center_sensors".equals(screen)
                || "center_places".equals(screen) || "center_photo".equals(screen);
    }

    public static void render(Host h, String screen) {
        if (!"center_find".equals(screen)) stopDiscovery();
        if ("center_overview".equals(screen)) overview(h);
        else if ("center_find".equals(screen)) find(h);
        else if ("center_sensors".equals(screen)) sensors(h);
        else if ("center_places".equals(screen)) places(h);
        else if ("center_photo".equals(screen)) photo(h);
        else hub(h);
    }

    /** Call when the app leaves the foreground or another screen opens. */
    public static void stopDiscovery() {
        if (discovery != null) {
            discovery.stop();
            discovery = null;
        }
    }

    private static void back(final Host h, LinearLayout box) {
        Button back = Ui.button(h.context(), "Back to Command center", Ui.PANEL);
        back.setOnClickListener(new android.view.View.OnClickListener() {
            @Override public void onClick(android.view.View v) { h.show("center"); }
        });
        box.addView(back);
    }

    // ------------------------------------------------------------------ hub

    private static void hub(final Host h) {
        Context c = h.context();
        LinearLayout card = Ui.card(c);
        card.addView(Ui.text(c, "Command center", 18, Ui.CYAN));
        card.addView(Ui.text(c, "What your PC reports, finding your PC on this network, and what this phone shares.", 12, Ui.DIM));
        h.content().addView(card);
        String[][] items = {{"center_overview", "Overview from the PC"}, {"center_find", "Find my PC on this network"},
                {"center_sensors", "Phone sensors and privacy"}, {"center_places", "Place alerts (arrive / leave)"},
                {"center_photo", "Take a photo for JARVIS"}};
        for (final String[] item : items) {
            Button b = Ui.button(c, item[1], Ui.PANEL);
            b.setOnClickListener(new android.view.View.OnClickListener() {
                @Override public void onClick(android.view.View v) { h.show(item[0]); }
            });
            h.content().addView(b);
        }
        Button tasks = Ui.button(c, "Background tasks", Ui.PANEL);
        tasks.setOnClickListener(new android.view.View.OnClickListener() {
            @Override public void onClick(android.view.View v) { h.show("missions"); }
        });
        h.content().addView(tasks);
        LinearLayout tiles = Ui.card(c);
        tiles.addView(Ui.text(c, "Quick Settings tiles", 15, Ui.CYAN));
        tiles.addView(Ui.text(c, "Swipe down twice, tap the pencil, and add \"JARVIS stop\" (emergency stop) and \"Wake PC\". Wake only "
                + "works for a PC that is asleep or hibernating and allows Wake-on-LAN; a fully shut-down PC needs firmware support.", 12, Ui.DIM));
        h.content().addView(tiles);
    }

    // ------------------------------------------------------------------ overview

    @SuppressWarnings("unchecked")
    private static void overview(final Host h) {
        final Context c = h.context();
        back(h, h.content());
        Pc pc = h.store().current();
        if (pc == null) {
            h.content().addView(Ui.text(c, "Pair a computer first (Devices).", 14, Ui.DIM));
            return;
        }
        final LinearLayout box = new LinearLayout(c);
        box.setOrientation(LinearLayout.VERTICAL);
        box.addView(Ui.text(c, "Asking the PC...", 14, Ui.DIM));
        h.content().addView(box);
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("since", "yesterday");
        h.actions().run(pc, h.away(), "overview", args, new Actions.Done() {
            @Override public void result(Api.Result result, Exception error) {
                box.removeAllViews();
                if (error != null || result == null) {
                    box.addView(Ui.text(c, "Couldn't reach the PC.", 14, Ui.GOLD));
                    return;
                }
                Map<String, Object> json = result.json();
                Object sections = json.get("sections");
                if (!(sections instanceof Map)) {
                    box.addView(Ui.text(c, "The PC didn't send an overview (it may need an update): "
                            + Json.str(json, "message", Json.str(json, "error", "status " + result.status)), 13, Ui.GOLD));
                    return;
                }
                Map<String, Object> s = (Map<String, Object>) sections;
                String[][] order = {{"situations", "Situations"}, {"security", "Security"}, {"tasks", "Cross-device tasks"}, {"changed", "What changed"}};
                for (String[] o : order) {
                    LinearLayout card = Ui.card(c);
                    card.addView(Ui.text(c, o[1], 15, Ui.CYAN));
                    String body = Json.str(s, o[0], "unavailable");
                    card.addView(Ui.text(c, body.isEmpty() ? "nothing to report" : body, 13, Ui.TEXT));
                    box.addView(card);
                }
            }
        });
    }

    // ------------------------------------------------------------------ find my PC

    private static void find(final Host h) {
        final Context c = h.context();
        back(h, h.content());
        LinearLayout info = Ui.card(c);
        info.addView(Ui.text(c, "Find my PC", 16, Ui.CYAN));
        info.addView(Ui.text(c, "Looks for JARVIS PCs on this Wi-Fi for about 8 seconds. Finding is not trusting: a PC you have not paired "
                + "is only listed. Pair it with the QR code on the PC (Menu > Mobile devices > Pair a new phone) and check the fingerprint "
                + "on both screens. If nothing is found, turn on Local discovery on the PC, or pair with the QR code directly.", 12, Ui.DIM));
        h.content().addView(info);
        final LinearLayout results = new LinearLayout(c);
        results.setOrientation(LinearLayout.VERTICAL);
        final TextView status = Ui.text(c, "Searching...", 13, Ui.DIM);
        h.content().addView(status);
        h.content().addView(results);
        stopDiscovery();
        discovery = new Discovery(c);
        discovery.start(new Discovery.Listener() {
            @Override public void onFound(List<Discovery.Found> all) {
                results.removeAllViews();
                for (final Discovery.Found f : all) {
                    LinearLayout card = Ui.card(c);
                    Pc known = null;
                    for (Pc pc : h.store().pcs()) if (Discovery.matches(pc, f)) known = pc;
                    card.addView(Ui.text(c, f.name + (f.version.isEmpty() ? "" : "  (v" + f.version + ")"), 16, Ui.TEXT));
                    card.addView(Ui.text(c, f.host + ":" + f.port, 12, Ui.DIM));
                    if (known != null) {
                        final Pc paired = known;
                        card.addView(Ui.text(c, "Your paired PC. Connections stay pinned to its certificate.", 12, Ui.GREEN));
                        if (!f.host.equals(paired.host) || f.port != paired.port) {
                            Button use = Ui.button(c, "Update its address to " + f.host + ":" + f.port, Ui.PANEL);
                            use.setOnClickListener(new android.view.View.OnClickListener() {
                                @Override public void onClick(android.view.View v) {
                                    paired.host = f.host;
                                    paired.port = f.port;
                                    h.store().upsert(paired);
                                    h.toast("Address updated.");
                                    h.show("center_find");
                                }
                            });
                            card.addView(use);
                        }
                    } else {
                        card.addView(Ui.text(c, "Not paired. Pair it from the PC (QR code); until then it has no access.", 12, Ui.GOLD));
                    }
                    results.addView(card);
                }
                status.setText(all.size() + " found so far...");
            }

            @Override public void onDone(String note) {
                status.setText(note.isEmpty() ? "Done." : note);
            }
        }, 8000);
    }

    // ------------------------------------------------------------------ phone sensors

    private static void sensors(final Host h) {
        final Context c = h.context();
        back(h, h.content());
        LinearLayout info = Ui.card(c);
        info.addView(Ui.text(c, "What this phone tells the PC", 16, Ui.CYAN));
        info.addView(Ui.text(c, "Each kind has its own switch, and only this phone can change them. Values are read once at a check-in, "
                + "summarised in one word, and sent: no raw sensor data, no location, nothing stored on the phone. Motion only says "
                + "\"stationary\" or \"moving\" and does not claim walking or driving. Check-ins happen when PC control is on, and "
                + "now and then otherwise.", 12, Ui.DIM));
        h.content().addView(info);
        LinearLayout switches = Ui.card(c);
        for (int i = 0; i < SensorHub.KINDS.length; i++) {
            final String kind = SensorHub.KINDS[i];
            CheckBox box = new CheckBox(c);
            box.setText(SensorHub.LABELS[i]);
            box.setTextColor(Ui.TEXT);
            box.setChecked(SensorHub.enabled(c, kind));
            box.setOnCheckedChangeListener(new android.widget.CompoundButton.OnCheckedChangeListener() {
                @Override public void onCheckedChanged(android.widget.CompoundButton b, boolean on) { SensorHub.setEnabled(c, kind, on); }
            });
            switches.addView(box);
        }
        h.content().addView(switches);
        LinearLayout have = Ui.card(c);
        have.addView(Ui.text(c, "Sensors in this phone", 14, Ui.CYAN));
        Map<String, Object> available = SensorHub.available(c);
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, Object> e : available.entrySet()) sb.append(Boolean.TRUE.equals(e.getValue()) ? "yes  " : "no   ").append(e.getKey()).append("\n");
        have.addView(Ui.text(c, sb.length() == 0 ? "unknown" : sb.toString().trim(), 13, Ui.TEXT));
        h.content().addView(have);
        final TextView preview = Ui.text(c, "", 13, Ui.TEXT);
        Button show = Ui.button(c, "Show what would be sent now", Ui.PANEL);
        show.setOnClickListener(new android.view.View.OnClickListener() {
            @Override public void onClick(android.view.View v) {
                preview.setText("Reading...");
                h.actions().runOnPool(new Runnable() {
                    @Override public void run() {
                        final String text = Json.write(SensorHub.context(c, true));
                        h.actions().onMain(new Runnable() { @Override public void run() { preview.setText(text); } });
                    }
                });
            }
        });
        h.content().addView(show);
        LinearLayout out = Ui.card(c);
        out.addView(preview);
        h.content().addView(out);
    }

    // ------------------------------------------------------------------ place alerts

    private static void places(final Host h) {
        final Context c = h.context();
        back(h, h.content());
        LinearLayout info = Ui.card(c);
        info.addView(Ui.text(c, "Place alerts", 16, Ui.CYAN));
        info.addView(Ui.text(c, "Your PC sends this phone a few places (home, work...). The phone decides by itself whether you arrived or left and tells the PC "
                + "only that - never where you are. It looks at the location about every 15 minutes (not live), needs two looks in a row to be sure, "
                + "so an alert can be up to about 30 minutes late. For this to work with the app closed, Android needs Location set to "
                + "\"Allow all the time\". Off by default; switching it off stops all looking.", 12, Ui.DIM));
        h.content().addView(info);
        LinearLayout box = Ui.card(c);
        CheckBox on = new CheckBox(c);
        on.setText("Place alerts");
        on.setTextColor(Ui.TEXT);
        on.setChecked(Places.enabled(c));
        on.setOnCheckedChangeListener(new android.widget.CompoundButton.OnCheckedChangeListener() {
            @Override public void onCheckedChanged(android.widget.CompoundButton b, boolean checked) {
                Places.setEnabled(c, checked);
                if (checked && !Places.hasLocationPermission(c)) {
                    h.permissions(new String[]{"android.permission.ACCESS_FINE_LOCATION"}, 6);
                } else if (checked && !Places.hasBackgroundPermission(c)) {
                    h.toast("Now set Location to \"Allow all the time\" in the app's Android settings so alerts work with the app closed.");
                    if (android.os.Build.VERSION.SDK_INT >= 29) h.permissions(new String[]{"android.permission.ACCESS_BACKGROUND_LOCATION"}, 7);
                }
            }
        });
        box.addView(on);
        h.content().addView(box);
        LinearLayout list = Ui.card(c);
        list.addView(Ui.text(c, "Places on this phone", 14, Ui.CYAN));
        StringBuilder sb = new StringBuilder();
        for (Map<String, Object> p : Places.list(c)) sb.append(Json.str(p, "name", "?")).append("  (").append(Math.round(((Number) p.get("radius_m")).doubleValue())).append(" m)\n");
        list.addView(Ui.text(c, sb.length() == 0 ? "None yet. On the PC say: \"send my places to my phone\"." : sb.toString().trim(), 13, Ui.TEXT));
        h.content().addView(list);
        final TextView status = Ui.text(c, Places.summary(c), 13, Ui.TEXT);
        Button look = Ui.button(c, "Look now", Ui.PANEL);
        look.setOnClickListener(new android.view.View.OnClickListener() {
            @Override public void onClick(android.view.View v) {
                status.setText("Looking...");
                h.actions().runOnPool(new Runnable() {
                    @Override public void run() {
                        Places.sample(c);
                        h.actions().onMain(new Runnable() { @Override public void run() { status.setText(Places.summary(c)); } });
                    }
                });
            }
        });
        h.content().addView(look);
        LinearLayout out = Ui.card(c);
        out.addView(status);
        h.content().addView(out);
    }

    // ------------------------------------------------------------------ photo for JARVIS

    private static void photo(final Host h) {
        Context c = h.context();
        back(h, h.content());
        LinearLayout info = Ui.card(c);
        info.addView(Ui.text(c, "Take a photo for JARVIS", 16, Ui.CYAN));
        info.addView(Ui.text(c, "Opens your camera app. You take one photo; it is shrunk and sent to your PC (like Share > JARVIS Mobile), and the copy on this phone is "
                + "removed. Then ask JARVIS \"what is this?\" and it looks at it on the PC. Nothing is taken without you pressing the shutter. Needs Android 10 or newer.", 12, Ui.DIM));
        h.content().addView(info);
        Button go = Ui.button(c, "Open the camera", Ui.PANEL);
        go.setOnClickListener(new android.view.View.OnClickListener() {
            @Override public void onClick(android.view.View v) { h.takePhoto(); }
        });
        h.content().addView(go);
    }
}
