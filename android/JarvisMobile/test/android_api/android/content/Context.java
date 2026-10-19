package android.content;
import android.content.pm.PackageManager;
import android.content.res.Resources;
public abstract class Context {
    public static final String JOB_SCHEDULER_SERVICE = "jobscheduler", NOTIFICATION_SERVICE = "notification",
            CAMERA_SERVICE = "camera", KEYGUARD_SERVICE = "keyguard", MEDIA_PROJECTION_SERVICE = "media_projection",
            CONNECTIVITY_SERVICE = "connectivity", TELECOM_SERVICE = "telecom", CLIPBOARD_SERVICE = "clipboard", DEVICE_POLICY_SERVICE = "device_policy",
            LOCATION_SERVICE = "location", WIFI_SERVICE = "wifi", AUDIO_SERVICE = "audio", VIBRATOR_SERVICE = "vibrator";
    public static final int MODE_PRIVATE = 0;
    public abstract SharedPreferences getSharedPreferences(String name, int mode);
    public abstract Object getSystemService(String name);
    public Intent registerReceiver(BroadcastReceiver receiver, IntentFilter filter) { return null; }
    public PackageManager getPackageManager() { return null; }
    public void startActivity(Intent intent) {}
    public String getPackageName() { return ""; }
    public Resources getResources() { return new Resources(); }
    public ComponentName startService(Intent i) { return null; }
    public ComponentName startForegroundService(Intent i) { return null; }
    public int checkSelfPermission(String p) { return 0; }
    public Context getApplicationContext() { return this; }
    public java.io.File getFilesDir() { return null; }
    public java.io.File getCacheDir() { return null; }
    public android.content.ContentResolver getContentResolver() { return null; }
    public <T> T getSystemService(Class<T> k) { return null; }
}
