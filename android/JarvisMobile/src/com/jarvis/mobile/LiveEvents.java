package com.jarvis.mobile;

import android.os.Handler;
import android.os.Looper;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Instant updates without Firebase: while the app is open, one signed "wait_events" request is kept open to the
 * PC, which answers the moment a message or an approval question arrives (or after ~20 s with nothing), and the
 * next request goes out at once. Off the UI thread, its own thread (never blocks the command queue). If the PC
 * can't be reached it backs off (2 s .. 30 s) and the activity's normal polling keeps working meanwhile.
 * Only while the app is open: in the background Android's job scheduler (SyncJob) still decides when to check.
 */
public final class LiveEvents {
    public interface Listener {
        void events(Map<String, Object> json);
    }

    public interface Cursor {
        long since();
        String pendingId();
    }

    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile boolean running = false;
    private volatile boolean connected = false;
    private Thread thread;

    public boolean connected() {
        return connected;
    }

    public synchronized void start(final Pc pc, final boolean away, final Cursor cursor, final Listener listener) {
        stop();
        if (pc == null) return;
        running = true;
        thread = new Thread(() -> loop(pc, away, cursor, listener), "jarvis-live");
        thread.setDaemon(true);
        thread.start();
    }

    public synchronized void stop() {
        running = false;
        connected = false;
        if (thread != null) thread.interrupt();
        thread = null;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asMap(Object o) {
        return (Map<String, Object>) o;
    }

    static long maxId(Object messages) {
        long max = 0;
        if (messages instanceof java.util.List) {
            for (Object o : (java.util.List<?>) messages) {
                if (o instanceof Map) {
                    Object id = ((Map<?, ?>) o).get("id");
                    if (id instanceof Number) max = Math.max(max, ((Number) id).longValue());
                }
            }
        }
        return max;
    }

    private void loop(Pc pc, boolean away, Cursor cursor, Listener listener) {
        long backoff = 2000;
        long since = cursor.since();
        String pendingId = cursor.pendingId();
        while (running) {
            try {
                since = Math.max(since, cursor.since());          // also what the normal polling already saw
                Map<String, Object> args = new LinkedHashMap<String, Object>();
                args.put("since", since);
                args.put("pending_id", pendingId);
                args.put("timeout", 20);
                Api.Result result = Api.command(pc, away, pc.id, "wait_events", args, 30000);
                if (result.status >= 400) throw new java.io.IOException("HTTP " + result.status);
                final Map<String, Object> json = result.json();
                connected = true;
                backoff = 2000;
                since = Math.max(since, maxId(json.get("messages")));   // the next request starts after these
                Object pending = json.get("pending");
                pendingId = pending instanceof Map ? Json.str(asMap(pending), "id", "") : "";
                if (Json.bool(json, "changed") && running) main.post(() -> listener.events(json));
            } catch (Exception e) {
                connected = false;
                if (!running) return;
                try {
                    Thread.sleep(backoff);
                } catch (InterruptedException stop) {
                    return;
                }
                backoff = Math.min(30000, backoff * 2);
            }
        }
    }
}
