package android.media.projection;
import android.hardware.display.VirtualDisplay;
import android.os.Handler;
public class MediaProjection {
    public abstract static class Callback { public void onStop() {} }
    public void registerCallback(Callback c, Handler h) {}
    public VirtualDisplay createVirtualDisplay(String n, int w, int h, int dpi, int flags, android.view.Surface s, Object cb, Handler hd) { return null; }
    public void stop() {}
}
