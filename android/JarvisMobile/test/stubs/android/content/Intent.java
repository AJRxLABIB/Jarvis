package android.content;
import android.net.Uri;
public class Intent {
    public static final String ACTION_MAIN = "android.intent.action.MAIN", ACTION_VIEW = "android.intent.action.VIEW",
            ACTION_BATTERY_CHANGED = "android.intent.action.BATTERY_CHANGED", CATEGORY_LAUNCHER = "android.intent.category.LAUNCHER",
            EXTRA_TEXT = "android.intent.extra.TEXT";
    public static final int FLAG_ACTIVITY_NEW_TASK = 0x10000000;
    public Intent() {}
    public Intent(Context c, Class<?> k) {}
    public Intent(String action) {}
    public Intent(String action, Uri uri) {}
    public Intent addCategory(String category) { return this; }
    public Intent addFlags(int flags) { return this; }
    public int getIntExtra(String name, int def) { return def; }
    public String getStringExtra(String name) { return null; }
}
