package android.view.accessibility;
public class AccessibilityNodeInfo { public static final int FOCUS_INPUT = 1, ACTION_SET_TEXT = 2097152;
    public static final String ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE = "ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE";
    public AccessibilityNodeInfo findFocus(int f) { return null; } public boolean isEditable() { return true; }
    public CharSequence getText() { return null; } public boolean performAction(int a, android.os.Bundle b) { return true; } }
