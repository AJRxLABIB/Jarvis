package android.service.notification;
import android.content.Context;
import android.content.SharedPreferences;
public abstract class NotificationListenerService extends Context {
    public void onNotificationPosted(StatusBarNotification sbn) {}
    public SharedPreferences getSharedPreferences(String name, int mode) { return null; }
    public Object getSystemService(String name) { return null; }
}
