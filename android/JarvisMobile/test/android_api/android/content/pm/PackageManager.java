package android.content.pm;
import android.content.Intent;
import java.util.List;
public abstract class PackageManager {
    public static final int PERMISSION_GRANTED = 0, PERMISSION_DENIED = -1, MATCH_DEFAULT_ONLY = 65536;
    public static class NameNotFoundException extends Exception {}
    public abstract List<ResolveInfo> queryIntentActivities(Intent intent, int flags);
    public abstract Intent getLaunchIntentForPackage(String packageName);
    public abstract ApplicationInfo getApplicationInfo(String packageName, int flags) throws NameNotFoundException;
    public abstract CharSequence getApplicationLabel(ApplicationInfo info);
    public PackageInfo getPackageInfo(String p, int f) throws NameNotFoundException { return null; }
}
