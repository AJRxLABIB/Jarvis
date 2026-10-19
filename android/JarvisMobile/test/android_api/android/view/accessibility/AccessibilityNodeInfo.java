package android.view.accessibility;
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
