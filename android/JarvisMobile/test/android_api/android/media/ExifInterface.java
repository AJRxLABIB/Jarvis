package android.media;
public class ExifInterface { public static final String TAG_ORIENTATION = "Orientation";
    public static final int ORIENTATION_NORMAL = 1, ORIENTATION_ROTATE_90 = 6, ORIENTATION_ROTATE_180 = 3, ORIENTATION_ROTATE_270 = 8;
    public ExifInterface(java.io.InputStream in) throws java.io.IOException {}
    public int getAttributeInt(String tag, int fallback) { return fallback; } }
