package android.net;
public abstract class Uri { public static Uri parse(String s) { return null; }
    public String getScheme() { return null; } public String getHost() { return null; } public String getQueryParameter(String k) { return null; }
    public String getPath() { return null; } public String getLastPathSegment() { return null; }
    public static String encode(String s) { return s; } public static Uri withAppendedPath(Uri base, String p) { return null; } }
