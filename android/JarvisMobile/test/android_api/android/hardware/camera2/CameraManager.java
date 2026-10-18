package android.hardware.camera2;
public final class CameraManager {
    public String[] getCameraIdList() throws CameraAccessException { return new String[0]; }
    public CameraCharacteristics getCameraCharacteristics(String id) throws CameraAccessException { return null; }
    public void setTorchMode(String id, boolean enabled) throws CameraAccessException {}
}
