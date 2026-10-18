package android.content;
import android.content.pm.PackageManager;
public abstract class Context {
    public static final String JOB_SCHEDULER_SERVICE = "jobscheduler", NOTIFICATION_SERVICE = "notification", MODE_PRIVATE_ = "";
    public static final String CAMERA_SERVICE = "camera";
    public static final int MODE_PRIVATE = 0;
    public abstract SharedPreferences getSharedPreferences(String name, int mode);
    public abstract Object getSystemService(String name);
    public Intent registerReceiver(BroadcastReceiver receiver, IntentFilter filter) { return null; }
    public PackageManager getPackageManager() { return null; }
    public void startActivity(Intent intent) {}
    public String getPackageName() { return ""; }
}
