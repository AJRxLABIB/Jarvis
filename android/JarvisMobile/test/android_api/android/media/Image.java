package android.media;
public abstract class Image implements AutoCloseable {
    public abstract static class Plane { public abstract java.nio.ByteBuffer getBuffer(); public abstract int getPixelStride(); public abstract int getRowStride(); }
    public abstract Plane[] getPlanes(); public abstract int getWidth(); public abstract int getHeight(); public abstract void close();
}
