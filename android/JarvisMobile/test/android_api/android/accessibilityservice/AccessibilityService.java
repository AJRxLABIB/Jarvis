package android.accessibilityservice;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
public abstract class AccessibilityService extends android.app.Service {
    public static final int GLOBAL_ACTION_BACK = 1, GLOBAL_ACTION_HOME = 2, GLOBAL_ACTION_RECENTS = 3;
    public static abstract class GestureResultCallback {}
    public abstract void onAccessibilityEvent(AccessibilityEvent e);
    public abstract void onInterrupt();
    protected void onServiceConnected() {}
    public final boolean performGlobalAction(int a) { return true; }
    public final boolean dispatchGesture(GestureDescription g, GestureResultCallback cb, android.os.Handler h) { return true; }
    public AccessibilityNodeInfo getRootInActiveWindow() { return null; }
    public final android.os.IBinder onBind(Intent i) { return null; }
}
