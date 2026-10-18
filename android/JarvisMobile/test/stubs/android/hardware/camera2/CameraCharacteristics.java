package android.hardware.camera2;
public final class CameraCharacteristics {
    public static final class Key<T> {}
    public static final Key<Boolean> FLASH_INFO_AVAILABLE = new Key<Boolean>();
    public <T> T get(Key<T> key) { return null; }
}
