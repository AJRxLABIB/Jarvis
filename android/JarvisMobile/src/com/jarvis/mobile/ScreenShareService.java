package com.jarvis.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Base64;
import android.util.DisplayMetrics;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * "Share screen with PC": sends this phone's screen to the paired JARVIS PC while you want it to.
 *
 * You start it in the app; Android shows its own "start recording or casting?" question every time
 * (MediaProjection) and a notification stays up while it runs - Stop is in the notification and in the app.
 * About 3 small JPEG frames a second (540 px wide) go to the PC over the pinned, signed connection
 * (/api/mobile/frame); an unchanged screen is re-sent only every 2 s. Nothing is stored on either side.
 * If you also allowed control (ControlService), the taps the PC sends back with each answer are carried out.
 */
public final class ScreenShareService extends Service {
    public static final String EXTRA_CODE = "code", EXTRA_DATA = "data", EXTRA_AWAY = "away";
    private static final String ACTION_STOP = "com.jarvis.mobile.STOP_SHARE";
    private static final String CHANNEL = "screen_share";
    private static final int NOTE_ID = 7301;
    private static final int WIDTH = 540;
    private static final long FRAME_MS = 330, RESEND_MS = 2000;
    private static volatile boolean sharing = false;

    private MediaProjection projection;
    private VirtualDisplay display;
    private ImageReader reader;
    private Thread worker;
    private volatile boolean running = false;
    private boolean finished = false;
    private boolean away = false;
    private final Handler main = new Handler(Looper.getMainLooper());

    public static boolean sharing() {
        return sharing;
    }

    public static void stop(Context c) {
        c.startService(new Intent(c, ScreenShareService.class).setAction(ACTION_STOP));
    }

    @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || ACTION_STOP.equals(intent.getAction())) {
            shutdown(true);
            return START_NOT_STICKY;
        }
        // Android 10+: the service must be in the foreground (type mediaProjection) BEFORE the projection is made
        Notification note = notification();
        if (Build.VERSION.SDK_INT >= 29) startForeground(NOTE_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        else startForeground(NOTE_ID, note);
        final Pc pc = new Store(this).current();
        int code = intent.getIntExtra(EXTRA_CODE, 0);
        Intent data = intent.getParcelableExtra(EXTRA_DATA);
        if (pc == null || data == null) {
            shutdown(false);
            return START_NOT_STICKY;
        }
        MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        projection = mpm.getMediaProjection(code, data);
        if (projection == null) {
            shutdown(false);
            return START_NOT_STICKY;
        }
        projection.registerCallback(new MediaProjection.Callback() {
            @Override public void onStop() { shutdown(true); }      // stopped by Android or the system UI
        }, main);
        DisplayMetrics dm = getResources().getDisplayMetrics();
        int width = Math.min(WIDTH, dm.widthPixels);
        int height = Math.max(2, Math.round(dm.heightPixels * (width / (float) dm.widthPixels)));
        reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
        display = projection.createVirtualDisplay("jarvis-share", width, height, dm.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.getSurface(), null, null);
        running = true;
        sharing = true;
        away = intent.getBooleanExtra(EXTRA_AWAY, false);
        finished = false;
        final boolean awayNow = away;
        worker = new Thread(() -> loop(pc, awayNow), "jarvis-share");
        worker.start();
        return START_NOT_STICKY;
    }

    private Notification notification() {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Screen sharing with your PC",
                    NotificationManager.IMPORTANCE_LOW));
            b = new Notification.Builder(this, CHANNEL);
        } else {
            b = new Notification.Builder(this);
        }
        PendingIntent stop = PendingIntent.getService(this, 1,
                new Intent(this, ScreenShareService.class).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE);
        return b.setContentTitle("Sharing your screen with your PC")
                .setContentText("JARVIS can see this screen. Tap Stop to end.")
                .setSmallIcon(android.R.drawable.ic_menu_view)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Stop", stop).build())
                .build();
    }

    private void loop(Pc pc, boolean away) {
        long lastSent = 0, lastHash = 0;
        int failures = 0;
        while (running) {
            long started = System.currentTimeMillis();
            try {
                Bitmap frame = grab();
                if (frame != null) {
                    long hash = quickHash(frame);
                    if (hash != lastHash || started - lastSent >= RESEND_MS) {
                        lastHash = hash;
                        lastSent = started;
                        handleAnswer(send(pc, away, frame));
                        failures = 0;
                    }
                    frame.recycle();
                }
            } catch (Exception e) {
                if (++failures >= 15) {         // ~5 s without the PC: stop rather than keep capturing for nobody
                    main.post(() -> shutdown(false));
                    return;
                }
            }
            long wait = FRAME_MS - (System.currentTimeMillis() - started);
            if (wait > 0) {
                try { Thread.sleep(wait); } catch (InterruptedException stop) { return; }
            }
        }
    }

    private Bitmap grab() {
        Image image = reader == null ? null : reader.acquireLatestImage();
        if (image == null) return null;
        try {
            Image.Plane plane = image.getPlanes()[0];
            ByteBuffer buffer = plane.getBuffer();
            int pixelStride = plane.getPixelStride(), rowStride = plane.getRowStride();
            int w = image.getWidth(), h = image.getHeight();
            int padded = w + (rowStride - pixelStride * w) / pixelStride;      // rows can be wider than the image
            Bitmap wide = Bitmap.createBitmap(padded, h, Bitmap.Config.ARGB_8888);
            wide.copyPixelsFromBuffer(buffer);
            if (padded == w) return wide;
            Bitmap exact = Bitmap.createBitmap(wide, 0, 0, w, h);
            wide.recycle();
            return exact;
        } finally {
            image.close();
        }
    }

    static long quickHash(Bitmap b) {
        long h = 1125899906842597L;
        int w = b.getWidth(), ht = b.getHeight();
        for (int y = 0; y < ht; y += Math.max(1, ht / 24)) {
            for (int x = 0; x < w; x += Math.max(1, w / 16)) h = 31 * h + b.getPixel(x, y);
        }
        return h;
    }

    private Map<String, Object> send(Pc pc, boolean away, Bitmap frame) throws Exception {
        ByteArrayOutputStream jpeg = new ByteArrayOutputStream();
        frame.compress(Bitmap.CompressFormat.JPEG, 60, jpeg);
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("jpeg", Base64.encodeToString(jpeg.toByteArray(), Base64.NO_WRAP));
        args.put("width", frame.getWidth());
        args.put("height", frame.getHeight());
        args.put("control", ControlService.active(this));
        Api.Result result = Api.post(pc, away, "/api/mobile/frame", Protocol.envelope(pc.id, "screen_frame", args), 10000);
        if (result.status >= 400) throw new java.io.IOException("HTTP " + result.status);
        return result.json();
    }

    @SuppressWarnings("unchecked")
    private void handleAnswer(Map<String, Object> answer) {
        if (Json.bool(answer, "stop")) {                 // stopped on the PC (viewer, emergency stop)
            main.post(() -> shutdown(false));
            return;
        }
        Object input = answer.get("input");
        if (!(input instanceof List) || !ControlService.active(this)) return;
        for (Object o : (List<Object>) input) {
            if (o instanceof Map) {
                final Map<String, Object> event = (Map<String, Object>) o;
                main.post(() -> ControlService.perform(ScreenShareService.this, event));
            }
        }
    }

    private void shutdown(boolean tellPc) {
        if (finished) return;                  // Stop, Android's own stop and the PC's stop can all arrive
        finished = true;
        running = false;
        sharing = false;
        if (worker != null) worker.interrupt();
        worker = null;
        if (display != null) display.release();
        display = null;
        if (reader != null) reader.close();
        reader = null;
        MediaProjection p = projection;
        projection = null;
        if (p != null) p.stop();
        if (tellPc) {
            final Pc pc = new Store(this).current();
            if (pc != null) {
                new Thread(() -> {
                    try {
                        Api.post(pc, away, "/api/mobile/frame",
                                Protocol.envelope(pc.id, "screen_stopped", new LinkedHashMap<String, Object>()), 5000);
                    } catch (Exception ignored) { /* the PC notices by itself after a few seconds */ }
                }, "jarvis-share-stop").start();
            }
        }
        if (Build.VERSION.SDK_INT >= 24) stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    @Override public void onDestroy() {
        if (running) shutdown(false);
        super.onDestroy();
    }
}
