package android.content;
import java.util.Map;
public interface SharedPreferences {
    String getString(String key, String def);
    boolean getBoolean(String key, boolean def);
    Map<String, ?> getAll();
    Editor edit();
    interface Editor { Editor putString(String k, String v); Editor putBoolean(String k, boolean v); Editor remove(String k); void apply(); }
}
