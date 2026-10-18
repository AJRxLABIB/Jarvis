package com.jarvis.mobile;

import android.app.Notification;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Forwards new notifications to the PC - only after you (1) gave JARVIS Mobile notification access in Android's
 * settings and (2) switched "Forward notifications" on in the app. Ongoing ones (music, downloads) and this app's
 * own are skipped; codes that look like one-time passwords are removed on the phone already.
 */
public final class NotifyForward extends NotificationListenerService {
    private long lastSent = 0;
    private String lastKey = "";

    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null || !PhoneActions.forwardNotifications(this) || sbn.isOngoing()) return;
        if (getPackageName().equals(sbn.getPackageName())) return;
        Notification n = sbn.getNotification();
        Bundle extras = n == null ? null : n.extras;
        if (extras == null) return;
        CharSequence title = extras.getCharSequence(Notification.EXTRA_TITLE);
        CharSequence text = extras.getCharSequence(Notification.EXTRA_TEXT);
        String key = sbn.getPackageName() + "|" + title + "|" + text;
        long now = System.currentTimeMillis();
        if (key.equals(lastKey) || now - lastSent < 1500) return;          // no floods, no repeats
        lastKey = key;
        lastSent = now;
        String app = sbn.getPackageName();
        try {
            PackageManager pm = getPackageManager();
            app = String.valueOf(pm.getApplicationLabel(pm.getApplicationInfo(sbn.getPackageName(), 0)));
        } catch (Exception ignored) {
            // keep the package name
        }
        final Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("app", app);
        args.put("title", hideCodes(title == null ? "" : title.toString()));
        args.put("text", hideCodes(text == null ? "" : text.toString()));
        final Pc pc = new Store(this).current();
        if (pc == null) return;
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    Api.command(pc, false, pc.id, "phone_notify", args);
                } catch (Exception e) {
                    try { Api.command(pc, true, pc.id, "phone_notify", args); } catch (Exception ignored) { /* PC offline */ }
                }
            }
        }).start();
    }

    static String hideCodes(String text) {
        return text.replaceAll("(?<!\\d)\\d{4,8}(?!\\d)", "••••");
    }
}
