package com.jarvis.mobile;

/** Test stub: the shape PhoneActions uses (the real one is an Android AccessibilityService). */
public final class ControlService {
    public static boolean running() { return false; }
    public static Object[] readScreen(android.content.Context c) { return new Object[]{false, "stub"}; }
    public static Object[] tapText(android.content.Context c, String t) { return new Object[]{false, "stub"}; }
}
