package android.view;
import android.content.Context;
import android.graphics.drawable.Drawable;
public class View {
    public static final int VISIBLE = 0, INVISIBLE = 4, GONE = 8;
    public interface OnClickListener { void onClick(View v); }
    public interface OnLongClickListener { boolean onLongClick(View v); }
    public interface OnTouchListener { boolean onTouch(View v, MotionEvent e); }
    public View(Context c) {}
    public void setOnClickListener(OnClickListener l) {}
    public void setOnLongClickListener(OnLongClickListener l) {}
    public void setOnTouchListener(OnTouchListener l) {}
    public void setPadding(int l, int t, int r, int b) {}
    public void setBackground(Drawable d) {}
    public void setBackgroundColor(int c) {}
    public void setLayoutParams(ViewGroup.LayoutParams p) {}
    public ViewGroup.LayoutParams getLayoutParams() { return null; }
    public void setVisibility(int v) {}
    public void setEnabled(boolean e) {}
    public void setAlpha(float a) {}
    public void setMinimumHeight(int h) {}
    public int getWidth() { return 0; } public int getHeight() { return 0; }
    public Context getContext() { return null; }
    public boolean post(Runnable r) { return true; }
    public boolean postDelayed(Runnable r, long d) { return true; }
    public void requestFocus() {}
    public void setContentDescription(CharSequence c) {}
    public void setElevation(float e) {}
}
