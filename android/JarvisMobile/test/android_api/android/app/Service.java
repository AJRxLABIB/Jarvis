package android.app;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.IBinder;
public abstract class Service extends Context {
    public static final int START_NOT_STICKY = 2, START_STICKY = 1, STOP_FOREGROUND_REMOVE = 1;
    public abstract IBinder onBind(Intent intent);
    public int onStartCommand(Intent intent, int flags, int startId) { return 0; }
    public void onCreate() {}
    public void onDestroy() {}
    public boolean onUnbind(Intent intent) { return false; }
    public final void startForeground(int id, Notification n) {}
    public final void startForeground(int id, Notification n, int type) {}
    public final void stopForeground(int flags) {}
    public final void stopSelf() {}
    public SharedPreferences getSharedPreferences(String name, int mode) { return null; }
    public Object getSystemService(String name) { return null; }
}
