package android.content;
import android.net.Uri;
import android.os.Bundle;
import android.os.Parcelable;
public class Intent implements Parcelable {
    public static final String ACTION_MAIN = "android.intent.action.MAIN", ACTION_VIEW = "android.intent.action.VIEW",
            ACTION_BATTERY_CHANGED = "android.intent.action.BATTERY_CHANGED", CATEGORY_LAUNCHER = "android.intent.category.LAUNCHER",
            EXTRA_TEXT = "android.intent.extra.TEXT", ACTION_SEND = "android.intent.action.SEND", EXTRA_SUBJECT = "s",
            ACTION_CALL = "android.intent.action.CALL", ACTION_DIAL = "android.intent.action.DIAL", ACTION_SENDTO = "android.intent.action.SENDTO";
    public static final int FLAG_ACTIVITY_NEW_TASK = 0x10000000, FLAG_ACTIVITY_CLEAR_TOP = 0x04000000;
    public Intent() {}
    public Intent(Context c, Class<?> k) {}
    public Intent(String action) {}
    public Intent(String action, Uri uri) {}
    public Intent addCategory(String category) { return this; }
    public Intent addFlags(int flags) { return this; }
    public Intent setFlags(int flags) { return this; }
    public Intent setAction(String a) { return this; }
    public Intent setType(String t) { return this; }
    public void removeExtra(String k) {}
    public ComponentName resolveActivity(android.content.pm.PackageManager pm) { return null; }
    public static Intent makeMainSelectorActivity(String action, String category) { return new Intent(); }
    public String getAction() { return null; }
    public Uri getData() { return null; }
    public String getType() { return null; }
    public Intent setData(Uri u) { return this; }
    public Intent setPackage(String p) { return this; }
    public int getIntExtra(String name, int def) { return def; }
    public boolean getBooleanExtra(String name, boolean def) { return def; }
    public String getStringExtra(String name) { return null; }
    public java.util.ArrayList<String> getStringArrayListExtra(String name) { return null; }
    public <T extends Parcelable> T getParcelableExtra(String name) { return null; }
    public Bundle getExtras() { return null; }
    public Intent putExtra(String k, int v) { return this; }
    public Intent putExtra(String k, boolean v) { return this; }
    public Intent putExtra(String k, String v) { return this; }
    public Intent putExtra(String k, long v) { return this; }
    public Intent putExtra(String k, Parcelable v) { return this; }
}
