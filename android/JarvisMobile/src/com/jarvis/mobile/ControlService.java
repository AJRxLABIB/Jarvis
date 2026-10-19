package com.jarvis.mobile;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Path;
import android.os.Bundle;
import android.util.DisplayMetrics;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.Map;

/**
 * "JARVIS control": carries out the taps, swipes and Back / Home / Recents you make in the PC's phone-screen
 * viewer, while you are sharing the screen. Two separate opt-ins are needed: you switch this service on in Android
 * Settings > Accessibility (Android warns you what it allows), AND "Allow the PC to tap" in the app. It keeps
 * nothing and sends nothing; it only performs gestures, and only those that came back with a screen frame
 * you are sharing (ScreenShareService). Typing reads only the focused text field, to add your text to it.
 */
public final class ControlService extends AccessibilityService {
    private static volatile ControlService instance;

    public static boolean running() {
        return instance != null;
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("screen_share", Context.MODE_PRIVATE);
    }

    public static boolean allowed(Context c) {
        return prefs(c).getBoolean("allow_taps", false);
    }

    public static void setAllowed(Context c, boolean on) {
        prefs(c).edit().putBoolean("allow_taps", on).apply();
    }

    /** Control is on only when the service runs AND you allowed it in the app. */
    public static boolean active(Context c) {
        return running() && allowed(c);
    }

    @Override protected void onServiceConnected() {
        instance = this;
    }

    @Override public boolean onUnbind(android.content.Intent intent) {
        instance = null;
        return super.onUnbind(intent);
    }

    @Override public void onDestroy() {
        instance = null;
        super.onDestroy();
    }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) { /* nothing is read */ }

    @Override public void onInterrupt() { }

    /** Carry out one input event from the PC (coordinates 0..1 of the screen). Returns false if not possible. */
    public static boolean perform(Context c, Map<String, Object> event) {
        ControlService s = instance;
        if (s == null || !allowed(c) || event == null) return false;
        String kind = Json.str(event, "kind", "");
        switch (kind) {
            case "back": return s.performGlobalAction(GLOBAL_ACTION_BACK);
            case "home": return s.performGlobalAction(GLOBAL_ACTION_HOME);
            case "recents": return s.performGlobalAction(GLOBAL_ACTION_RECENTS);
            case "tap": {
                float[] p = s.point(event, "x", "y");
                Path path = new Path();
                path.moveTo(p[0], p[1]);
                long ms = Json.bool(event, "long") ? 800 : 60;
                return s.gesture(path, ms);
            }
            case "swipe": {
                float[] a = s.point(event, "x", "y");
                float[] b = s.point(event, "x2", "y2");
                Path path = new Path();
                path.moveTo(a[0], a[1]);
                path.lineTo(b[0], b[1]);
                long ms = (long) number(event, "ms", 300);
                return s.gesture(path, Math.max(50, Math.min(2000, ms)));
            }
            case "text": return s.typeText(Json.str(event, "text", ""));
            default: return false;
        }
    }

    private static double number(Map<String, Object> m, String key, double fallback) {
        Object v = m.get(key);
        return v instanceof Number ? ((Number) v).doubleValue() : fallback;
    }

    /** The text on the screen now (other apps included), for "what's on my phone": visible texts and buttons, in
     *  reading order. Password fields are never read; apps that block screenshots usually block this too. */
    public static Object[] readScreen(Context c) {
        ControlService s = instance;
        if (s == null || !allowed(c)) return new Object[]{false, "turn on 'JARVIS control' (Accessibility) and 'Allow the PC to tap' on the phone"};
        AccessibilityNodeInfo root = s.getRootInActiveWindow();
        if (root == null) return new Object[]{false, "the phone didn't give the screen's contents (a protected app?)"};
        StringBuilder out = new StringBuilder();
        CharSequence pkg = root.getPackageName();
        if (pkg != null) out.append("[").append(pkg).append("] ");
        collect(root, out, new int[]{0});
        String text = out.toString().trim();
        return new Object[]{true, text.length() > 1900 ? text.substring(0, 1900) + "..." : text};
    }

    private static void collect(AccessibilityNodeInfo node, StringBuilder out, int[] count) {
        if (node == null || count[0] > 150 || out.length() > 2000) return;
        if (node.isVisibleToUser()) {
            CharSequence t = node.isPassword() ? null : node.getText();
            CharSequence d = node.getContentDescription();
            String piece = t != null && t.length() > 0 ? t.toString() : d != null ? d.toString() : "";
            if (!piece.trim().isEmpty()) {
                out.append(node.isClickable() ? "[" + piece.trim() + "]" : piece.trim()).append(" | ");
                count[0]++;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) collect(node.getChild(i), out, count);
    }

    /** Tap the button / item whose text (or description) contains ``wanted`` - by the screen's own structure, not
     *  by a remembered position, so it still works when things moved. */
    public static Object[] tapText(Context c, String wanted) {
        ControlService s = instance;
        if (s == null || !allowed(c)) return new Object[]{false, "turn on 'JARVIS control' (Accessibility) and 'Allow the PC to tap' on the phone"};
        AccessibilityNodeInfo root = s.getRootInActiveWindow();
        if (root == null || wanted.trim().isEmpty()) return new Object[]{false, "nothing to tap"};
        AccessibilityNodeInfo node = find(root, wanted.trim().toLowerCase(java.util.Locale.ROOT));
        if (node == null) return new Object[]{false, "no '" + wanted + "' on the phone's screen now"};
        AccessibilityNodeInfo target = node;
        while (target != null && !target.isClickable()) target = target.getParent();
        if (target != null && target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            return new Object[]{true, "Tapped '" + wanted + "'."};
        }
        android.graphics.Rect r = new android.graphics.Rect();
        node.getBoundsInScreen(r);
        Path path = new Path();
        path.moveTo(r.exactCenterX(), r.exactCenterY());
        boolean ok = s.gesture(path, 60);
        return new Object[]{ok, ok ? "Tapped '" + wanted + "'." : "the phone refused the tap"};
    }

    private static AccessibilityNodeInfo find(AccessibilityNodeInfo node, String wanted) {
        if (node == null) return null;
        if (node.isVisibleToUser() && !node.isPassword()) {
            CharSequence t = node.getText();
            CharSequence d = node.getContentDescription();
            if ((t != null && t.toString().toLowerCase(java.util.Locale.ROOT).contains(wanted))
                    || (d != null && d.toString().toLowerCase(java.util.Locale.ROOT).contains(wanted))) return node;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo hit = find(node.getChild(i), wanted);
            if (hit != null) return hit;
        }
        return null;
    }

    private float[] point(Map<String, Object> event, String kx, String ky) {
        DisplayMetrics dm = getResources().getDisplayMetrics();
        double x = Math.max(0, Math.min(1, number(event, kx, 0.5)));
        double y = Math.max(0, Math.min(1, number(event, ky, 0.5)));
        return new float[]{(float) (x * (dm.widthPixels - 1)), (float) (y * (dm.heightPixels - 1))};
    }

    private boolean gesture(Path path, long ms) {
        GestureDescription.Builder b = new GestureDescription.Builder();
        b.addStroke(new GestureDescription.StrokeDescription(path, 0, ms));
        return dispatchGesture(b.build(), null, null);
    }

    private boolean typeText(String text) {
        if (text.isEmpty()) return false;
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return false;
        AccessibilityNodeInfo focus = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (focus == null || !focus.isEditable()) return false;
        CharSequence before = focus.getText();
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                (before == null ? "" : before.toString()) + text);
        return focus.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
    }
}
