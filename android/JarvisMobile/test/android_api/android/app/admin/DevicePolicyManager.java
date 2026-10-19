package android.app.admin;
import android.content.ComponentName;
public class DevicePolicyManager {
    public static final String ACTION_ADD_DEVICE_ADMIN = "x", EXTRA_DEVICE_ADMIN = "y", EXTRA_ADD_EXPLANATION = "z";
    public boolean isAdminActive(ComponentName c) { return false; }
    public void lockNow() {}
}
