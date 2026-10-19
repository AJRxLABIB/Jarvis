"""Writes test/android_api: compile-only stand-ins for the Android classes the app uses (signatures only, no
behaviour), so every source file - MainActivity and the services included - can be compiled with a plain JDK.
Usage: python test/make_android_api.py test/android_api. A real build still uses android.jar (build_android.ps1)."""
import os, sys
root = sys.argv[1]
S = {}
def c(path, body): S[path] = body

c("android/content/Context.java", """package android.content;
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
""")
c("android/content/ContentResolver.java", """package android.content;
public class ContentResolver { public android.database.Cursor query(android.net.Uri u, String[] p, String s, String[] a, String o) { return null; }
    public java.io.InputStream openInputStream(android.net.Uri u) throws java.io.FileNotFoundException { return null; } }
""")
c("android/content/ActivityNotFoundException.java", "package android.content;\npublic class ActivityNotFoundException extends RuntimeException {}\n")
c("android/database/Cursor.java", """package android.database;
public interface Cursor extends java.io.Closeable { boolean moveToNext(); boolean moveToFirst(); String getString(int i); long getLong(int i); void close(); }
""")
c("android/provider/ContactsContract.java", """package android.provider;
public final class ContactsContract {
    public static final class CommonDataKinds { public static final class Phone {
        public static final android.net.Uri CONTENT_URI = null; public static final String DISPLAY_NAME = "display_name", NUMBER = "data1"; } }
    public static final class PhoneLookup { public static final android.net.Uri CONTENT_FILTER_URI = null; public static final String DISPLAY_NAME = "display_name"; }
}
""")
c("android/app/admin/DeviceAdminReceiver.java", "package android.app.admin;\npublic class DeviceAdminReceiver extends android.content.BroadcastReceiver { public void onReceive(android.content.Context c, android.content.Intent i) {} }\n")
c("android/app/admin/DevicePolicyManager.java", """package android.app.admin;
import android.content.ComponentName;
public class DevicePolicyManager {
    public static final String ACTION_ADD_DEVICE_ADMIN = "x", EXTRA_DEVICE_ADMIN = "y", EXTRA_ADD_EXPLANATION = "z";
    public boolean isAdminActive(ComponentName c) { return false; }
    public void lockNow() {}
}
""")
c("android/telecom/TelecomManager.java", "package android.telecom;\npublic class TelecomManager { public boolean endCall() { return true; } }\n")
c("android/telephony/SmsManager.java", """package android.telephony;
public class SmsManager { public static SmsManager getDefault() { return new SmsManager(); }
    public java.util.ArrayList<String> divideMessage(String t) { return new java.util.ArrayList<String>(); }
    public void sendMultipartTextMessage(String to, String sc, java.util.ArrayList<String> parts, java.util.ArrayList<android.app.PendingIntent> s, java.util.ArrayList<android.app.PendingIntent> d) {} }
""")
c("android/graphics/Rect.java", "package android.graphics;\npublic final class Rect { public int left, top, right, bottom; public float exactCenterX() { return 0; } public float exactCenterY() { return 0; } }\n")
c("android/content/ClipboardManager.java", """package android.content;
public class ClipboardManager { public void setPrimaryClip(ClipData c) {} public ClipData getPrimaryClip() { return null; } }
""")
c("android/content/ClipData.java", """package android.content;
public class ClipData { public static ClipData newPlainText(CharSequence l, CharSequence t) { return new ClipData(); }
    public int getItemCount() { return 0; } public Item getItemAt(int i) { return new Item(); }
    public static class Item { public CharSequence coerceToText(Context c) { return ""; } } }
""")
c("android/location/Location.java", """package android.location;
public class Location { public double getLatitude() { return 0; } public double getLongitude() { return 0; }
    public float getAccuracy() { return 0; } public long getTime() { return 0; } }
""")
c("android/location/LocationManager.java", """package android.location;
public class LocationManager { public java.util.List<String> getProviders(boolean enabled) { return new java.util.ArrayList<String>(); }
    public Location getLastKnownLocation(String p) { return null; } }
""")
c("android/content/res/Resources.java", """package android.content.res;
public class Resources { public android.util.DisplayMetrics getDisplayMetrics() { return new android.util.DisplayMetrics(); } }
""")
c("android/content/Intent.java", """package android.content;
import android.net.Uri;
import android.os.Bundle;
import android.os.Parcelable;
public class Intent implements Parcelable {
    public static final String ACTION_MAIN = "android.intent.action.MAIN", ACTION_VIEW = "android.intent.action.VIEW",
            ACTION_BATTERY_CHANGED = "android.intent.action.BATTERY_CHANGED", CATEGORY_LAUNCHER = "android.intent.category.LAUNCHER",
            EXTRA_TEXT = "android.intent.extra.TEXT", ACTION_SEND = "android.intent.action.SEND", EXTRA_SUBJECT = "s",
            EXTRA_STREAM = "android.intent.extra.STREAM",
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
""")
c("android/os/Parcelable.java", "package android.os;\npublic interface Parcelable {}\n")
c("android/os/IBinder.java", "package android.os;\npublic interface IBinder {}\n")
c("android/os/Build.java", """package android.os;
public class Build {
    public static String MANUFACTURER = "", MODEL = "", BRAND = "", DEVICE = "";
    public static class VERSION { public static int SDK_INT = 34; public static String RELEASE = ""; }
    public static class VERSION_CODES { public static final int N = 24, O = 26, P = 28, Q = 29, R = 30, S = 31, TIRAMISU = 33, UPSIDE_DOWN_CAKE = 34; }
}
""")
c("android/os/Handler.java", """package android.os;
public class Handler {
    public Handler() {}
    public Handler(Looper looper) {}
    public final boolean post(Runnable r) { return true; }
    public final boolean postDelayed(Runnable r, long delayMillis) { return true; }
    public final void removeCallbacks(Runnable r) {}
}
""")
c("android/os/Bundle.java", """package android.os;
public final class Bundle {
    public CharSequence getCharSequence(String key) { return null; }
    public void putCharSequence(String key, CharSequence v) {}
    public String getString(String key) { return null; }
    public void putString(String k, String v) {}
}
""")
c("android/app/Activity.java", """package android.app;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.View;
public class Activity extends Context {
    public static final int RESULT_OK = -1, RESULT_CANCELED = 0;
    protected void onCreate(Bundle state) {}
    protected void onResume() {}
    protected void onPause() {}
    protected void onDestroy() {}
    protected void onNewIntent(Intent intent) {}
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {}
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {}
    public void startActivityForResult(Intent intent, int code) {}
    public void requestPermissions(String[] p, int code) {}
    public Intent getIntent() { return null; }
    public void setIntent(Intent i) {}
    public void finish() {}
    public void setContentView(View v) {}
    public final void runOnUiThread(Runnable action) {}
    public android.view.Window getWindow() { return null; }
    public SharedPreferences getSharedPreferences(String name, int mode) { return null; }
    public Object getSystemService(String name) { return null; }
}
""")
c("android/view/Window.java", """package android.view;
public class Window { public void setStatusBarColor(int c) {} public void setNavigationBarColor(int c) {} public void addFlags(int f) {} public View getDecorView() { return null; } }
""")
c("android/app/Service.java", """package android.app;
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
""")
c("android/content/pm/ServiceInfo.java", "package android.content.pm;\npublic class ServiceInfo { public static final int FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION = 32, FOREGROUND_SERVICE_TYPE_SPECIAL_USE = 1073741824; }\n")
c("android/app/Notification.java", """package android.app;
import android.content.Context;
import android.os.Bundle;
public class Notification {
    public static final String EXTRA_TITLE = "android.title", EXTRA_TEXT = "android.text";
    public Bundle extras;
    public static class Action {
        public static class Builder {
            public Builder(android.graphics.drawable.Icon icon, CharSequence title, PendingIntent intent) {}
            public Action build() { return new Action(); }
        }
    }
    public static class Builder {
        public Builder(Context c) {}
        public Builder(Context c, String channel) {}
        public Builder setSmallIcon(int i) { return this; }
        public Builder setContentTitle(CharSequence t) { return this; }
        public Builder setContentText(CharSequence t) { return this; }
        public Builder setContentIntent(PendingIntent p) { return this; }
        public Builder setAutoCancel(boolean b) { return this; }
        public Builder setOngoing(boolean b) { return this; }
        public Builder addAction(Action a) { return this; }
        public Notification build() { return new Notification(); }
    }
}
""")
c("android/graphics/drawable/Icon.java", "package android.graphics.drawable;\npublic class Icon {}\n")
c("android/app/NotificationManager.java", """package android.app;
public class NotificationManager { public static final int IMPORTANCE_DEFAULT = 3, IMPORTANCE_LOW = 2, IMPORTANCE_HIGH = 4;
    public void createNotificationChannel(NotificationChannel c) {} public void notify(int id, Notification n) {} public void cancel(int id) {} }
""")
c("android/app/PendingIntent.java", """package android.app;
import android.content.Context;
import android.content.Intent;
public class PendingIntent { public static final int FLAG_IMMUTABLE = 1, FLAG_UPDATE_CURRENT = 2;
    public static PendingIntent getActivity(Context c, int code, Intent i, int flags) { return null; }
    public static PendingIntent getService(Context c, int code, Intent i, int flags) { return null; } }
""")
c("android/app/KeyguardManager.java", """package android.app;
import android.content.Intent;
public class KeyguardManager { public Intent createConfirmDeviceCredentialIntent(CharSequence t, CharSequence d) { return null; }
    public boolean isDeviceSecure() { return true; } public boolean isKeyguardLocked() { return false; } }
""")
c("android/app/AlertDialog.java", """package android.app;
import android.content.Context;
import android.content.DialogInterface;
import android.view.View;
public class AlertDialog implements DialogInterface {
    public void dismiss() {} public void show() {} public void cancel() {}
    public static class Builder {
        public Builder(Context c) {}
        public Builder setTitle(CharSequence t) { return this; }
        public Builder setMessage(CharSequence t) { return this; }
        public Builder setView(View v) { return this; }
        public Builder setPositiveButton(CharSequence t, DialogInterface.OnClickListener l) { return this; }
        public Builder setNegativeButton(CharSequence t, DialogInterface.OnClickListener l) { return this; }
        public Builder setNeutralButton(CharSequence t, DialogInterface.OnClickListener l) { return this; }
        public Builder setItems(CharSequence[] items, DialogInterface.OnClickListener l) { return this; }
        public Builder setOnDismissListener(DialogInterface.OnDismissListener l) { return this; }
        public Builder setCancelable(boolean b) { return this; }
        public AlertDialog create() { return new AlertDialog(); }
        public AlertDialog show() { return new AlertDialog(); }
    }
}
""")
c("android/content/DialogInterface.java", """package android.content;
public interface DialogInterface {
    interface OnClickListener { void onClick(DialogInterface d, int which); }
    interface OnDismissListener { void onDismiss(DialogInterface d); }
    void dismiss();
    void cancel();
}
""")
c("android/content/ComponentName.java", "package android.content;\npublic class ComponentName implements android.os.Parcelable { public ComponentName(String p, String c) {} public ComponentName(Context c, Class<?> k) {} public String getPackageName() { return \"\"; } public String getClassName() { return \"\"; } }\n")
c("android/graphics/Color.java", """package android.graphics;
public class Color { public static final int BLACK = 0xff000000, WHITE = -1, TRANSPARENT = 0;
    public static int rgb(int r, int g, int b) { return 0; } public static int argb(int a, int r, int g, int b) { return 0; } public static int parseColor(String s) { return 0; } }
""")
c("android/graphics/drawable/Drawable.java", "package android.graphics.drawable;\npublic abstract class Drawable {}\n")
c("android/graphics/drawable/GradientDrawable.java", """package android.graphics.drawable;
public class GradientDrawable extends Drawable { public void setColor(int c) {} public void setCornerRadius(float r) {} public void setStroke(int w, int c) {} }
""")
c("android/graphics/Path.java", "package android.graphics;\npublic class Path { public void moveTo(float x, float y) {} public void lineTo(float x, float y) {} }\n")
c("android/graphics/PixelFormat.java", "package android.graphics;\npublic class PixelFormat { public static final int RGBA_8888 = 1; }\n")
c("android/graphics/Bitmap.java", """package android.graphics;
public class Bitmap {
    public enum Config { ARGB_8888 }
    public enum CompressFormat { JPEG, PNG }
    public static Bitmap createBitmap(int w, int h, Config c) { return new Bitmap(); }
    public static Bitmap createBitmap(Bitmap src, int x, int y, int w, int h) { return new Bitmap(); }
    public static Bitmap createScaledBitmap(Bitmap src, int w, int h, boolean f) { return new Bitmap(); }
    public void copyPixelsFromBuffer(java.nio.Buffer b) {}
    public boolean compress(CompressFormat f, int q, java.io.OutputStream o) { return true; }
    public int getWidth() { return 0; } public int getHeight() { return 0; } public int getPixel(int x, int y) { return 0; }
    public void recycle() {}
}
""")
c("android/hardware/display/DisplayManager.java", "package android.hardware.display;\npublic class DisplayManager { public static final int VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR = 16; }\n")
c("android/hardware/display/VirtualDisplay.java", "package android.hardware.display;\npublic class VirtualDisplay { public void release() {} }\n")
c("android/view/Surface.java", "package android.view;\npublic class Surface {}\n")
c("android/media/Image.java", """package android.media;
public abstract class Image implements AutoCloseable {
    public abstract static class Plane { public abstract java.nio.ByteBuffer getBuffer(); public abstract int getPixelStride(); public abstract int getRowStride(); }
    public abstract Plane[] getPlanes(); public abstract int getWidth(); public abstract int getHeight(); public abstract void close();
}
""")
c("android/media/ImageReader.java", """package android.media;
public class ImageReader { public static ImageReader newInstance(int w, int h, int f, int max) { return new ImageReader(); }
    public Image acquireLatestImage() { return null; } public android.view.Surface getSurface() { return null; } public void close() {} }
""")
c("android/media/projection/MediaProjection.java", """package android.media.projection;
import android.hardware.display.VirtualDisplay;
import android.os.Handler;
public class MediaProjection {
    public abstract static class Callback { public void onStop() {} }
    public void registerCallback(Callback c, Handler h) {}
    public VirtualDisplay createVirtualDisplay(String n, int w, int h, int dpi, int flags, android.view.Surface s, Object cb, Handler hd) { return null; }
    public void stop() {}
}
""")
c("android/media/projection/MediaProjectionManager.java", """package android.media.projection;
import android.content.Intent;
public class MediaProjectionManager { public Intent createScreenCaptureIntent() { return null; } public MediaProjection getMediaProjection(int code, Intent data) { return null; } }
""")
c("android/util/Base64.java", """package android.util;
public class Base64 { public static final int DEFAULT = 0, NO_WRAP = 2, URL_SAFE = 8, NO_PADDING = 1;
    public static String encodeToString(byte[] b, int f) { return ""; } public static byte[] decode(String s, int f) { return new byte[0]; } }
""")
c("android/util/DisplayMetrics.java", "package android.util;\npublic class DisplayMetrics { public int widthPixels, heightPixels, densityDpi; public float density; }\n")
c("android/util/TypedValue.java", """package android.util;
public class TypedValue { public static final int COMPLEX_UNIT_DIP = 1, COMPLEX_UNIT_SP = 2;
    public static float applyDimension(int unit, float v, DisplayMetrics m) { return v; } }
""")
c("android/util/Log.java", "package android.util;\npublic class Log { public static int w(String t, String m) { return 0; } public static int d(String t, String m) { return 0; } }\n")
c("android/view/Gravity.java", "package android.view;\npublic class Gravity { public static final int CENTER = 17, CENTER_HORIZONTAL = 1, CENTER_VERTICAL = 16, START = 8388611, END = 8388613, LEFT = 3, RIGHT = 5, TOP = 48, BOTTOM = 80; }\n")
c("android/view/MotionEvent.java", "package android.view;\npublic class MotionEvent { public static final int ACTION_DOWN = 0, ACTION_UP = 1, ACTION_CANCEL = 3; public int getAction() { return 0; } public final int getActionMasked() { return 0; } public float getX() { return 0; } public float getY() { return 0; } }\n")
c("android/view/View.java", """package android.view;
import android.content.Context;
import android.graphics.drawable.Drawable;
public class View {
    public static final int VISIBLE = 0, INVISIBLE = 4, GONE = 8;
    public interface OnClickListener { void onClick(View v); }
    public interface OnLongClickListener { boolean onLongClick(View v); }
    public interface OnTouchListener { boolean onTouch(View v, MotionEvent e); }
    public View(Context c) {}
    public void setOnClickListener(OnClickListener l) {}
    public void setOnLongClickListener(OnLongClickListener l) {}
    public void setOnTouchListener(OnTouchListener l) {}
    public void setPadding(int l, int t, int r, int b) {}
    public void setBackground(Drawable d) {}
    public void setBackgroundColor(int c) {}
    public void setLayoutParams(ViewGroup.LayoutParams p) {}
    public ViewGroup.LayoutParams getLayoutParams() { return null; }
    public void setVisibility(int v) {}
    public void setEnabled(boolean e) {}
    public void setAlpha(float a) {}
    public void setMinimumHeight(int h) {}
    public int getWidth() { return 0; } public int getHeight() { return 0; }
    public Context getContext() { return null; }
    public boolean post(Runnable r) { return true; }
    public boolean postDelayed(Runnable r, long d) { return true; }
    public void requestFocus() {}
    public void setContentDescription(CharSequence c) {}
    public void setElevation(float e) {}
}
""")
c("android/view/ViewGroup.java", """package android.view;
import android.content.Context;
public class ViewGroup extends View {
    public static class LayoutParams { public static final int MATCH_PARENT = -1, WRAP_CONTENT = -2; public int width, height; public LayoutParams(int w, int h) {} }
    public static class MarginLayoutParams extends LayoutParams { public int leftMargin, topMargin, rightMargin, bottomMargin;
        public MarginLayoutParams(int w, int h) { super(w, h); } public void setMargins(int l, int t, int r, int b) {} }
    public ViewGroup(Context c) { super(c); }
    public void addView(View v) {}
    public void addView(View v, LayoutParams p) {}
    public void addView(View v, int i) {}
    public void removeAllViews() {}
    public void removeView(View v) {}
    public int getChildCount() { return 0; }
    public View getChildAt(int i) { return null; }
}
""")
c("android/widget/LinearLayout.java", """package android.widget;
import android.content.Context;
import android.view.ViewGroup;
public class LinearLayout extends ViewGroup {
    public static final int VERTICAL = 1, HORIZONTAL = 0;
    public static class LayoutParams extends ViewGroup.MarginLayoutParams {
        public float weight; public int gravity;
        public LayoutParams(int w, int h) { super(w, h); } public LayoutParams(int w, int h, float weight) { super(w, h); } }
    public LinearLayout(Context c) { super(c); }
    public void setOrientation(int o) {}
    public void setGravity(int g) {}
}
""")
c("android/widget/FrameLayout.java", """package android.widget;
import android.content.Context;
import android.view.ViewGroup;
public class FrameLayout extends ViewGroup { public FrameLayout(Context c) { super(c); } }
""")
c("android/widget/ScrollView.java", """package android.widget;
import android.content.Context;
public class ScrollView extends FrameLayout { public ScrollView(Context c) { super(c); } public void fullScroll(int d) {} public void setFillViewport(boolean b) {} }
""")
c("android/widget/TextView.java", """package android.widget;
import android.content.Context;
import android.view.View;
public class TextView extends View {
    public TextView(Context c) { super(c); }
    public void setText(CharSequence s) {}
    public CharSequence getText() { return ""; }
    public void setTextSize(float s) {}
    public void setTextSize(int unit, float s) {}
    public void setTextColor(int c) {}
    public void setGravity(int g) {}
    public void setAllCaps(boolean b) {}
    public void setLetterSpacing(float s) {}
    public void setTypeface(android.graphics.Typeface t) {}
    public void setTypeface(android.graphics.Typeface t, int style) {}
    public void setSingleLine(boolean b) {}
    public void setMaxLines(int n) {}
    public void setHint(CharSequence h) {}
    public void setHintTextColor(int c) {}
    public void setInputType(int t) {}
    public void setTextIsSelectable(boolean b) {}
    public void setLineSpacing(float a, float m) {}
}
""")
c("android/graphics/Typeface.java", "package android.graphics;\npublic class Typeface { public static final Typeface DEFAULT_BOLD = null, MONOSPACE = null, DEFAULT = null; public static final int BOLD = 1, NORMAL = 0; }\n")
c("android/widget/Button.java", "package android.widget;\nimport android.content.Context;\npublic class Button extends TextView { public Button(Context c) { super(c); } }\n")
c("android/widget/EditText.java", """package android.widget;
import android.content.Context;
public class EditText extends TextView { public EditText(Context c) { super(c); }
    public android.text.Editable getText() { return null; } public void setSelection(int i) {} }
""")
c("android/text/Editable.java", "package android.text;\npublic interface Editable extends CharSequence {}\n")
c("android/text/InputType.java", "package android.text;\npublic class InputType { public static final int TYPE_CLASS_TEXT = 1, TYPE_CLASS_NUMBER = 2, TYPE_TEXT_VARIATION_URI = 16, TYPE_TEXT_VARIATION_PASSWORD = 128, TYPE_TEXT_FLAG_MULTI_LINE = 131072; }\n")
c("android/widget/ImageView.java", """package android.widget;
import android.content.Context;
import android.view.View;
public class ImageView extends View { public ImageView(Context c) { super(c); }
    public void setImageBitmap(android.graphics.Bitmap b) {} public void setAdjustViewBounds(boolean b) {}
    public enum ScaleType { FIT_CENTER, CENTER_CROP } public void setScaleType(ScaleType s) {} }
""")
c("android/graphics/BitmapFactory.java", "package android.graphics;\npublic class BitmapFactory { public static Bitmap decodeByteArray(byte[] b, int o, int l) { return null; } }\n")
c("android/speech/RecognizerIntent.java", """package android.speech;
public class RecognizerIntent { public static final String ACTION_RECOGNIZE_SPEECH = "a", EXTRA_LANGUAGE_MODEL = "b", LANGUAGE_MODEL_FREE_FORM = "c",
    EXTRA_RESULTS = "d", EXTRA_PROMPT = "e", EXTRA_LANGUAGE = "f", EXTRA_MAX_RESULTS = "g"; }
""")
c("android/provider/Settings.java", """package android.provider;
public class Settings { public static final String ACTION_ACCESSIBILITY_SETTINGS = "android.settings.ACCESSIBILITY_SETTINGS",
    ACTION_NOTIFICATION_LISTENER_SETTINGS = "android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS", ACTION_SETTINGS = "android.settings.SETTINGS"; }
""")
c("android/security/keystore/KeyProperties.java", """package android.security.keystore;
public class KeyProperties { public static final String KEY_ALGORITHM_EC = "EC", DIGEST_SHA256 = "SHA-256";
    public static final int PURPOSE_SIGN = 4, PURPOSE_VERIFY = 8, AUTH_BIOMETRIC_STRONG = 1, AUTH_DEVICE_CREDENTIAL = 2; }
""")
c("android/security/keystore/KeyGenParameterSpec.java", """package android.security.keystore;
public class KeyGenParameterSpec implements java.security.spec.AlgorithmParameterSpec {
    public static class Builder {
        public Builder(String alias, int purposes) {}
        public Builder setAlgorithmParameterSpec(java.security.spec.AlgorithmParameterSpec s) { return this; }
        public Builder setDigests(String... d) { return this; }
        public Builder setUserAuthenticationRequired(boolean b) { return this; }
        public Builder setUserAuthenticationValidityDurationSeconds(int s) { return this; }
        public Builder setUserAuthenticationParameters(int s, int types) { return this; }
        public Builder setInvalidatedByBiometricEnrollment(boolean b) { return this; }
        public Builder setIsStrongBoxBacked(boolean b) { return this; }
        public KeyGenParameterSpec build() { return new KeyGenParameterSpec(); }
    }
}
""")
c("android/security/keystore/KeyInfo.java", "package android.security.keystore;\npublic class KeyInfo implements java.security.spec.KeySpec { public boolean isInsideSecureHardware() { return true; } public boolean isUserAuthenticationRequired() { return true; } }\n")
c("android/accessibilityservice/AccessibilityService.java", """package android.accessibilityservice;
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
""")
c("android/accessibilityservice/GestureDescription.java", """package android.accessibilityservice;
import android.graphics.Path;
public final class GestureDescription {
    public static class StrokeDescription { public StrokeDescription(Path p, long start, long duration) {} }
    public static class Builder { public Builder addStroke(StrokeDescription s) { return this; } public GestureDescription build() { return new GestureDescription(); } }
}
""")
c("android/view/accessibility/AccessibilityEvent.java", "package android.view.accessibility;\npublic class AccessibilityEvent {}\n")
c("android/view/accessibility/AccessibilityNodeInfo.java", """package android.view.accessibility;
public class AccessibilityNodeInfo { public static final int FOCUS_INPUT = 1, ACTION_SET_TEXT = 2097152;
    public static final String ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE = "ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE";
    public static final int ACTION_CLICK = 16;
    public AccessibilityNodeInfo findFocus(int f) { return null; } public boolean isEditable() { return true; }
    public CharSequence getText() { return null; } public boolean performAction(int a, android.os.Bundle b) { return true; }
    public boolean performAction(int a) { return true; } public CharSequence getPackageName() { return null; }
    public boolean isVisibleToUser() { return true; } public boolean isPassword() { return false; }
    public CharSequence getContentDescription() { return null; } public boolean isClickable() { return false; }
    public int getChildCount() { return 0; } public AccessibilityNodeInfo getChild(int i) { return null; }
    public AccessibilityNodeInfo getParent() { return null; } public void getBoundsInScreen(android.graphics.Rect r) {} }
""")
c("android/R.java", """package android;
public final class R { public static final class drawable { public static final int ic_dialog_info = 1, ic_menu_view = 2, stat_notify_sync = 3; }
    public static final class style { public static final int Theme_Material_NoActionBar = 1; } }
""")
c("android/content/pm/PackageManager.java", """package android.content.pm;
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
""")
c("android/content/pm/PackageInfo.java", "package android.content.pm;\npublic class PackageInfo { public String versionName; }\n")
c("android/net/Uri.java", """package android.net;
public abstract class Uri { public static Uri parse(String s) { return null; }
    public String getScheme() { return null; } public String getHost() { return null; } public String getQueryParameter(String k) { return null; }
    public String getPath() { return null; } public String getLastPathSegment() { return null; }
    public static String encode(String s) { return s; } public static Uri withAppendedPath(Uri base, String p) { return null; } }
""")
c("android/Manifest.java", "package android;\npublic final class Manifest { public static final class permission { public static final String RECORD_AUDIO = \"a\", POST_NOTIFICATIONS = \"b\"; } }\n")

import shutil
stubs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stubs")
for part in ("android", "org"):            # start from the JVM test stand-ins, then add / replace with the ones above
    if os.path.isdir(os.path.join(stubs, part)):
        shutil.copytree(os.path.join(stubs, part), os.path.join(root, part), dirs_exist_ok=True)
for path, body in S.items():
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w").write(body)
print(len(S))
