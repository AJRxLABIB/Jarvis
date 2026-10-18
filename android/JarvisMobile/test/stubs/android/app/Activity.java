package android.app;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
public class Activity extends Context {
    protected void onCreate(Bundle state) {}
    public Intent getIntent() { return null; }
    public void finish() {}
    public final void runOnUiThread(Runnable action) {}
    public SharedPreferences getSharedPreferences(String name, int mode) { return null; }
    public Object getSystemService(String name) { return null; }
}
