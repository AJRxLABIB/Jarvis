package com.jarvis.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

/**
 * "Stay connected" (opt-in): keeps the link to your PC open while JARVIS Mobile is closed, so the PC's requests
 * (call, message, open the gallery, start screen sharing...) run within seconds instead of only while the app is
 * on screen. Android requires a visible notification for this; Stop is in it and in the app. Uses a little battery.
 */
public final class LinkService extends Service {
    private static final String ACTION_STOP = "com.jarvis.mobile.STOP_LINK";
    private static final String CHANNEL = "pc_link";
    private static final int NOTE_ID = 7302;
    private static final long EVERY_MS = 3000;
    private static volatile boolean running = false;
    private Thread worker;

    public static boolean running() {
        return running;
    }

    public static void start(Context c) {
        Intent i = new Intent(c, LinkService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    public static void stop(Context c) {
        c.startService(new Intent(c, LinkService.class).setAction(ACTION_STOP));
    }

    @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            running = false;
            if (worker != null) worker.interrupt();
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return START_NOT_STICKY;
        }
        Notification note = notification();
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTE_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTE_ID, note);
        if (!running) {
            running = true;
            worker = new Thread(this::loop, "jarvis-link");
            worker.start();
        }
        return START_STICKY;                      // Android restarts it if it had to stop it
    }

    private void loop() {
        long wait = EVERY_MS;
        while (running) {
            Pc pc = new Store(this).current();
            try {
                if (pc != null && PhoneActions.controlAllowed(this)) PhoneActions.checkIn(this, pc, false);
                wait = EVERY_MS;
            } catch (Exception offline) {
                wait = Math.min(60000, wait * 2);          // the PC is off / away: try less often
            }
            try { Thread.sleep(wait); } catch (InterruptedException stop) { return; }
        }
    }

    private Notification notification() {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Connected to your PC",
                    NotificationManager.IMPORTANCE_LOW));
            b = new Notification.Builder(this, CHANNEL);
        } else {
            b = new Notification.Builder(this);
        }
        PendingIntent stop = PendingIntent.getService(this, 2,
                new Intent(this, LinkService.class).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE);
        return b.setContentTitle("JARVIS: connected to your PC")
                .setContentText("Your PC can ask this phone to do things you allowed. Tap Stop to disconnect.")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Stop", stop).build())
                .build();
    }

    @Override public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }
}
