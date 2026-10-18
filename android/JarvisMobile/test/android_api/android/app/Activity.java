package android.app;
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
