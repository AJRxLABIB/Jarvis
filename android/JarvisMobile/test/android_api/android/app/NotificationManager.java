package android.app;
public class NotificationManager { public static final int IMPORTANCE_DEFAULT = 3, IMPORTANCE_LOW = 2, IMPORTANCE_HIGH = 4;
    public void createNotificationChannel(NotificationChannel c) {} public void notify(int id, Notification n) {} public void cancel(int id) {} }
