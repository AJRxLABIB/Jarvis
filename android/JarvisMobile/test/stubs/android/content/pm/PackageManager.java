package android.content.pm;
import android.content.Intent;
import java.util.List;
public abstract class PackageManager {
    public static class NameNotFoundException extends Exception {}
    public abstract List<ResolveInfo> queryIntentActivities(Intent intent, int flags);
    public abstract Intent getLaunchIntentForPackage(String packageName);
    public abstract ApplicationInfo getApplicationInfo(String packageName, int flags) throws NameNotFoundException;
    public abstract CharSequence getApplicationLabel(ApplicationInfo info);
}
