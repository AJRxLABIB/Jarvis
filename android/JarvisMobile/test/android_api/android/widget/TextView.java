package android.widget;
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
