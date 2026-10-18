package android.graphics;
public class Bitmap {
    public enum Config { ARGB_8888 }
    public enum CompressFormat { JPEG, PNG }
    public static Bitmap createBitmap(int w, int h, Config c) { return new Bitmap(); }
    public static Bitmap createBitmap(Bitmap src, int x, int y, int w, int h) { return new Bitmap(); }
    public static Bitmap createScaledBitmap(Bitmap src, int w, int h, boolean f) { return new Bitmap(); }
    public void copyPixelsFromBuffer(java.nio.Buffer b) {}
    public boolean compress(CompressFormat f, int q, java.io.OutputStream o) { return true; }
    public int getWidth() { return 0; } public int getHeight() { return 0; } public int getPixel(int x, int y) { return 0; }
    public void recycle() {}
}
