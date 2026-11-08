package com.jarvis.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;
import android.media.Ringtone;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * What the PC may ask THIS phone to do, and only after you switched "Let my PC control this phone" on here
 * (the PC can't switch it on). Flashlight, ring (find my phone), open an app or a web link, show a message.
 * Android rules still apply: opening an app or a link works while JARVIS Mobile is on screen (Android blocks apps
 * from opening others from the background); the flashlight, ringing and messages work in the background too.
 */
public final class PhoneActions {
    private PhoneActions() {}

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("phone_link", Context.MODE_PRIVATE);
    }

    public static boolean controlAllowed(Context c) { return prefs(c).getBoolean("control", false); }
    public static void setControlAllowed(Context c, boolean on) { prefs(c).edit().putBoolean("control", on).apply(); }
    public static boolean forwardNotifications(Context c) { return prefs(c).getBoolean("forward", false); }
    /** Calls, SMS, reading messages and contacts: a SEPARATE switch from "control" (and Android's own permissions). */
    public static boolean commsAllowed(Context c) { return prefs(c).getBoolean("comms", false); }
    public static void setCommsAllowed(Context c, boolean on) { prefs(c).edit().putBoolean("comms", on).apply(); }
    public static boolean stayConnected(Context c) { return prefs(c).getBoolean("stay", false); }
    public static void setStayConnected(Context c, boolean on) { prefs(c).edit().putBoolean("stay", on).apply(); }

    /** Android permissions the comms actions use (asked for from the app, never silently). */
    public static final String[] COMMS_PERMISSIONS = {"android.permission.CALL_PHONE", "android.permission.SEND_SMS",
            "android.permission.READ_SMS", "android.permission.READ_CONTACTS", "android.permission.ANSWER_PHONE_CALLS"};
    public static void setForwardNotifications(Context c, boolean on) { prefs(c).edit().putBoolean("forward", on).apply(); }

    /** Battery level, charging and the user's choices: sent with every check-in. */
    public static Map<String, Object> report(Context c) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        Intent battery = c.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (battery != null) {
            int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
            int scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
            int status = battery.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            if (level >= 0 && scale > 0) out.put("battery", Math.round(100f * level / scale));
            out.put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL);
        }
        out.put("control", controlAllowed(c));
        out.put("notifications", forwardNotifications(c));
        out.put("comms", commsAllowed(c));
        out.put("lock", LockAdmin.active(c));
        out.put("app_version", MainActivity.APP_VERSION);
        Map<String, Object> context = SensorHub.context(c, SensorHub.mayBlock());   // only the kinds switched on in "Phone sensors"
        if (!context.isEmpty()) out.put("context", context);
        out.put("sensors", SensorHub.available(c));
        return out;
    }

    /** Runs one action; returns {ok, message}. Never throws. */
    public static Object[] run(Context c, String action, Map<String, Object> args) {
        if (!controlAllowed(c)) return new Object[]{false, "control from the PC is switched off on the phone"};
        try {
            if ("flashlight_on".equals(action) || "flashlight_off".equals(action)) return torch(c, "flashlight_on".equals(action));
            if ("ring".equals(action)) return ring(c, intArg(args, "seconds", 15));
            if ("open_app".equals(action)) return openApp(c, Json.str(args, "app", ""));
            if ("open_link".equals(action)) return openLink(c, Json.str(args, "url", ""));
            if ("show_text".equals(action)) return show(c, Json.str(args, "text", ""));
            if ("open_gallery".equals(action)) return openGallery(c);
            if ("open_camera".equals(action)) return start(c, new Intent("android.media.action.STILL_IMAGE_CAMERA"), "Opened the camera.");
            if ("open_messages".equals(action)) return start(c, Intent.makeMainSelectorActivity(Intent.ACTION_MAIN,
                    "android.intent.category.APP_MESSAGING"), "Opened Messages.");
            if ("open_dialer".equals(action)) return start(c, new Intent(Intent.ACTION_DIAL), "Opened the phone dialer.");
            if ("share_screen".equals(action)) {
                Intent ask = new Intent(c, MainActivity.class).putExtra(MainActivity.EXTRA_START_SHARE, true);
                return start(c, ask, "Asked on the phone to start sharing - tap 'Start now' there.");
            }
            if ("read_screen".equals(action)) return ControlService.readScreen(c);
            if ("tap_text".equals(action)) return ControlService.tapText(c, Json.str(args, "text", ""));
            if ("set_clipboard".equals(action)) return setClipboard(c, Json.str(args, "text", ""));
            if ("locate".equals(action)) return locate(c);
            if ("lock_phone".equals(action)) return LockAdmin.lockNow(c);
            if (Comms.ACTIONS.contains(action)) {
                if (!commsAllowed(c)) return new Object[]{false, "calls and messages from the PC are switched off on the phone "
                        + "(JARVIS Mobile > Control > 'Allow calls and messages')"};
                return Comms.run(c, action, args);
            }
            return new Object[]{false, "this phone app doesn't know '" + action + "' (update JARVIS Mobile)"};
        } catch (Exception e) {
            return new Object[]{false, e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()};
        }
    }

    private static int intArg(Map<String, Object> args, String key, int fallback) {
        Object v = args == null ? null : args.get(key);
        return v instanceof Number ? ((Number) v).intValue() : fallback;
    }

    private static Object[] torch(Context c, boolean on) throws Exception {
        CameraManager cameras = (CameraManager) c.getSystemService(Context.CAMERA_SERVICE);
        for (String id : cameras.getCameraIdList()) {
            Boolean flash = cameras.getCameraCharacteristics(id).get(CameraCharacteristics.FLASH_INFO_AVAILABLE);
            if (Boolean.TRUE.equals(flash)) {
                cameras.setTorchMode(id, on);
                return new Object[]{true, on ? "Flashlight on." : "Flashlight off."};
            }
        }
        return new Object[]{false, "this phone has no flashlight"};
    }

    private static Object[] ring(Context c, int seconds) {
        Uri sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
        if (sound == null) sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
        final Ringtone tone = RingtoneManager.getRingtone(c, sound);
        if (tone == null) return new Object[]{false, "no ringtone available"};
        tone.play();
        new Handler(Looper.getMainLooper()).postDelayed(new Runnable() {
            @Override public void run() { tone.stop(); }
        }, Math.max(3, Math.min(seconds, 60)) * 1000L);
        return new Object[]{true, "The phone is ringing."};
    }

    private static Object[] openApp(Context c, String name) {
        PackageManager pm = c.getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN);
        launcher.addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> apps = pm.queryIntentActivities(launcher, 0);
        String wanted = name.toLowerCase(Locale.ROOT).trim();
        String bestPackage = null;
        int bestScore = 0;
        for (ResolveInfo info : apps) {
            String label = String.valueOf(info.loadLabel(pm)).toLowerCase(Locale.ROOT);
            int score = label.equals(wanted) ? 3 : label.startsWith(wanted) ? 2 : label.contains(wanted) ? 1 : 0;
            if (score > bestScore) { bestScore = score; bestPackage = info.activityInfo.packageName; }
        }
        if (bestPackage == null) return new Object[]{false, "no app called '" + name + "' on the phone"};
        Intent open = pm.getLaunchIntentForPackage(bestPackage);
        if (open == null) return new Object[]{false, "that app can't be opened directly"};
        open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        c.startActivity(open);
        return new Object[]{true, "Opened " + name + " on the phone."};
    }

    /** Start an activity from the PC's request. Android blocks this from the background unless JARVIS Mobile is on
     * screen or its "JARVIS control" accessibility service is on (then Android allows it). */
    static Object[] start(Context c, Intent intent, String done) {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            c.startActivity(intent);
        } catch (android.content.ActivityNotFoundException e) {
            return new Object[]{false, "no app on the phone can do that"};
        }
        return new Object[]{true, done + (ControlService.running() ? "" : " (If nothing opened: turn on 'JARVIS control' "
                + "in Accessibility, or keep JARVIS Mobile open.)")};
    }

    private static Object[] setClipboard(Context c, String text) {
        android.content.ClipboardManager cm = (android.content.ClipboardManager) c.getSystemService(Context.CLIPBOARD_SERVICE);
        if (cm == null || text.isEmpty()) return new Object[]{false, "no clipboard"};
        cm.setPrimaryClip(android.content.ClipData.newPlainText("JARVIS", text));
        return new Object[]{true, "On the phone's clipboard."};
    }

    /** The phone's last known location (GPS / network), only to your PC. Needs Location allowed for the app. */
    private static Object[] locate(Context c) {
        boolean fine = Build.VERSION.SDK_INT < 23
                || c.checkSelfPermission("android.permission.ACCESS_FINE_LOCATION") == PackageManager.PERMISSION_GRANTED
                || c.checkSelfPermission("android.permission.ACCESS_COARSE_LOCATION") == PackageManager.PERMISSION_GRANTED;
        if (!fine) return new Object[]{false, "allow Location for JARVIS Mobile first (Control > Allow location)"};
        android.location.LocationManager lm = (android.location.LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        android.location.Location best = null;
        for (String provider : lm.getProviders(true)) {
            android.location.Location l = lm.getLastKnownLocation(provider);
            if (l != null && (best == null || l.getTime() > best.getTime())) best = l;
        }
        if (best == null) return new Object[]{false, "the phone doesn't know its location yet (open Maps once)"};
        long minutes = Math.max(0, (System.currentTimeMillis() - best.getTime()) / 60000);
        return new Object[]{true, String.format(java.util.Locale.US,
                "%.5f, %.5f (within %d m, %d min ago) https://www.openstreetmap.org/?mlat=%.5f&mlon=%.5f#map=17/%.5f/%.5f",
                best.getLatitude(), best.getLongitude(), (int) best.getAccuracy(), minutes,
                best.getLatitude(), best.getLongitude(), best.getLatitude(), best.getLongitude())};
    }

    private static Object[] openGallery(Context c) {
        Intent gallery = Intent.makeMainSelectorActivity(Intent.ACTION_MAIN, "android.intent.category.APP_GALLERY");
        if (gallery.resolveActivity(c.getPackageManager()) == null) {
            gallery = new Intent(Intent.ACTION_VIEW);
            gallery.setType("image/*");
        }
        return start(c, gallery, "Opened the gallery.");
    }

    private static Object[] openLink(Context c, String url) {
        String lower = url.toLowerCase(Locale.ROOT);
        if (!(lower.startsWith("https://") || lower.startsWith("http://"))) return new Object[]{false, "only web links"};
        Intent view = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
        view.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        c.startActivity(view);
        return new Object[]{true, "Opened the link on the phone."};
    }

    private static Object[] show(Context c, String text) {
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel("jarvis_pc", "Notices from your PC",
                    NotificationManager.IMPORTANCE_DEFAULT));
        }
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, "jarvis_pc") : new Notification.Builder(c);
        b.setSmallIcon(android.R.drawable.ic_dialog_info).setContentTitle("JARVIS").setContentText(text).setAutoCancel(true);
        nm.notify((int) (System.currentTimeMillis() & 0xffff) + 5000, b.build());
        return new Object[]{true, "Shown on the phone."};
    }

    /**
     * One check-in with the PC: report this phone's state, run what the PC queued (if allowed here), report results.
     * Call from a background thread. Returns how many actions ran.
     */
    @SuppressWarnings("unchecked")
    public static int checkIn(final Context c, Pc pc, boolean away) throws Exception {
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("report", report(c));
        Api.Result result = Api.command(pc, away, pc.id, "phone_commands", args);
        if (result == null || result.status >= 400) return 0;
        Object list = result.json().get("commands");
        if (!(list instanceof List)) return 0;
        int ran = 0;
        for (Object o : (List<Object>) list) {
            if (!(o instanceof Map)) continue;
            final Map<String, Object> command = (Map<String, Object>) o;
            Object idValue = command.get("id");
            if (!(idValue instanceof Number)) continue;
            final String action = Json.str(command, "action", "");
            Object rawArgs = command.get("args");
            final Map<String, Object> actionArgs = rawArgs instanceof Map ? (Map<String, Object>) rawArgs
                    : new LinkedHashMap<String, Object>();
            final Object[][] outcome = new Object[1][];
            if (Looper.myLooper() == Looper.getMainLooper()) {
                outcome[0] = run(c, action, actionArgs);
            } else {                                         // starting activities / ringing belongs on the main thread
                final Object lock = new Object();
                new Handler(Looper.getMainLooper()).post(new Runnable() {
                    @Override public void run() {
                        Object[] r = PhoneActions.run(c, action, actionArgs);
                        synchronized (lock) { outcome[0] = r; lock.notifyAll(); }
                    }
                });
                synchronized (lock) {
                    long end = System.currentTimeMillis() + 5000;
                    while (outcome[0] == null && System.currentTimeMillis() < end) lock.wait(250);
                }
                if (outcome[0] == null) outcome[0] = new Object[]{false, "timed out on the phone"};
            }
            Map<String, Object> answer = new LinkedHashMap<String, Object>();
            answer.put("id", ((Number) idValue).longValue());
            answer.put("ok", outcome[0][0]);
            answer.put("message", outcome[0][1]);
            answer.put("unsupported", String.valueOf(outcome[0][1]).startsWith("this phone app doesn't know"));
            Api.command(pc, away, pc.id, "phone_result", answer);
            ran++;
        }
        return ran;
    }
}
