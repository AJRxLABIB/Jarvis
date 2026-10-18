package android.app;
import android.content.Context;
import android.content.Intent;
public class PendingIntent { public static final int FLAG_IMMUTABLE = 1, FLAG_UPDATE_CURRENT = 2;
    public static PendingIntent getActivity(Context c, int code, Intent i, int flags) { return null; }
    public static PendingIntent getService(Context c, int code, Intent i, int flags) { return null; } }
