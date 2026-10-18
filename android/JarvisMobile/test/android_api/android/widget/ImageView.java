package android.widget;
import android.content.Context;
import android.view.View;
public class ImageView extends View { public ImageView(Context c) { super(c); }
    public void setImageBitmap(android.graphics.Bitmap b) {} public void setAdjustViewBounds(boolean b) {}
    public enum ScaleType { FIT_CENTER, CENTER_CROP } public void setScaleType(ScaleType s) {} }
