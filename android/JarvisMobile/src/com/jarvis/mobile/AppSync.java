package com.jarvis.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Map;

/** Android glue for SyncClient: one local copy per paired PC, kept in this app's private storage. */
public final class AppSync {
    private AppSync() {}

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences("jarvis_sync", Context.MODE_PRIVATE);
    }

    public static SyncClient client(Context context, final Pc pc, final boolean away) {
        final SharedPreferences p = prefs(context);
        final String prefix = pc.id + ":";
        SyncClient.KeyValue kv = new SyncClient.KeyValue() {
            @Override public String get(String key, String fallback) { return p.getString(prefix + key, fallback); }
            @Override public void put(String key, String value) { p.edit().putString(prefix + key, value).apply(); }
        };
        return new SyncClient(kv, new SyncClient.Transport() {
            @Override public Api.Result command(String command, Map<String, Object> args) throws Exception {
                return Api.command(pc, away, pc.id, command, args);
            }
        });
    }

    /** Background checks are opt-in (Settings > Sync). */
    public static boolean backgroundOn(Context context) {
        return prefs(context).getBoolean("background", false);
    }

    public static void setBackground(Context context, boolean on) {
        prefs(context).edit().putBoolean("background", on).apply();
        if (on) SyncJob.schedule(context); else SyncJob.cancel(context);
    }

    /** Forget the local copy (and the cursor) for one PC. */
    public static void forget(Context context, Pc pc) {
        SharedPreferences p = prefs(context);
        SharedPreferences.Editor e = p.edit();
        for (String key : p.getAll().keySet()) if (key.startsWith(pc.id + ":")) e.remove(key);
        e.apply();
    }
}
