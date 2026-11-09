package android.hardware;
public class SensorManager { public static final int SENSOR_DELAY_GAME = 1;
    public Sensor getDefaultSensor(int type) { return null; }
    public boolean registerListener(SensorEventListener l, Sensor s, int rate, android.os.Handler h) { return true; }
    public void unregisterListener(SensorEventListener l) {} }
