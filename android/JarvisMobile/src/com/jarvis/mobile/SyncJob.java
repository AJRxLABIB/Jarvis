package com.jarvis.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.app.PendingIntent;
import android.os.Build;

import java.util.List;

/**
 * Opt-in background check (about every 15 minutes, only on a network, Android decides the exact time): pulls to-do
 * changes and asks the PC for generic notices ("A reminder is due."). It never sends edits (that needs your
 * fingerprint / PIN) and shows only the PC's generic sentence. This is polling, NOT push: a delivery can be late.
 */
public final class SyncJob extends JobService {
    private static final int JOB_ID = 4711;
    private static final String CHANNEL = "jarvis_pc";

    public static void schedule(Context context) {
        JobScheduler scheduler = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        JobInfo job = new JobInfo.Builder(JOB_ID, new ComponentName(context, SyncJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(15 * 60 * 1000L)
                .setPersisted(false)
                .build();
        scheduler.schedule(job);
    }

    public static void cancel(Context context) {
        ((JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE)).cancel(JOB_ID);
    }

    @Override public boolean onStartJob(final JobParameters params) {
        new Thread(new Runnable() {
            @Override public void run() {
                boolean retry = false;
                try {
                    Pc pc = new Store(SyncJob.this).current();
                    if (pc != null && AppSync.backgroundOn(SyncJob.this) && PhoneActions.controlAllowed(SyncJob.this)) {
                        try { PhoneActions.checkIn(SyncJob.this, pc, false); } catch (Exception ignored) { /* later */ }
                    }
                    if (pc != null && AppSync.backgroundOn(SyncJob.this)) {
                        SyncClient client = AppSync.client(SyncJob.this, pc, false);
                        List<String> notices = client.pollNotifications();
                        client.pullOnly();   // never sends edits from the background (they need your fingerprint / PIN)
                        if (!notices.isEmpty()) show(notices);
                    }
                } catch (Exception e) {
                    retry = !SyncClient.isAuthError(e);   // the PC is off / away: try again later; auth needs the user
                }
                jobFinished(params, retry);
            }
        }).start();
        return true;
    }

    @Override public boolean onStopJob(JobParameters params) { return true; }

    private void show(List<String> notices) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Notices from your PC", NotificationManager.IMPORTANCE_DEFAULT));
        }
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent tap = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        String text = notices.size() == 1 ? notices.get(0) : notices.size() + " notices from your PC.";
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.ic_dialog_info).setContentTitle("JARVIS").setContentText(text)
                .setContentIntent(tap).setAutoCancel(true);
        nm.notify(JOB_ID, b.build());
    }
}
