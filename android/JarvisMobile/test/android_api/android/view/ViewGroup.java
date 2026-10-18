package android.view;
import android.content.Context;
public class ViewGroup extends View {
    public static class LayoutParams { public static final int MATCH_PARENT = -1, WRAP_CONTENT = -2; public int width, height; public LayoutParams(int w, int h) {} }
    public static class MarginLayoutParams extends LayoutParams { public int leftMargin, topMargin, rightMargin, bottomMargin;
        public MarginLayoutParams(int w, int h) { super(w, h); } public void setMargins(int l, int t, int r, int b) {} }
    public ViewGroup(Context c) { super(c); }
    public void addView(View v) {}
    public void addView(View v, LayoutParams p) {}
    public void addView(View v, int i) {}
    public void removeAllViews() {}
    public void removeView(View v) {}
    public int getChildCount() { return 0; }
    public View getChildAt(int i) { return null; }
}
