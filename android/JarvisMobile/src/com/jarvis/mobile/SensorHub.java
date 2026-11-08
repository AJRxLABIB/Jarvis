package com.jarvis.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * What this phone may tell your PC about its surroundings - coarse context, never raw sensor streams, never location.
 *
 * Every kind has its OWN switch here on the phone (the PC cannot change them). Defaults: network and headphones ON (no
 * sensor is read), motion / light / screen orientation OFF. Nothing is logged or stored on the phone: a value is read
 * once at each check-in with the PC, summarised in one word, and sent. Motion is only "stationary" or "moving" from a
 * 1.5 second accelerometer sample - it does NOT claim walking, driving or any exact activity, which a single sample
 * cannot tell reliably. A sensor the phone doesn't have is reported as unavailable, never guessed.
 */
public final class SensorHub {
    private SensorHub() {}

    public static final String[] KINDS = {"network", "headset", "orientation", "motion", "light"};
    public static final String[] LABELS = {"Network type (Wi-Fi / mobile)", "Headphones connected", "Screen orientation",
            "Motion (still or moving)", "Ambient light (dark / dim / bright)"};

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("sensor_privacy", Context.MODE_PRIVATE);
    }

    public static boolean enabled(Context c, String kind) {
        boolean defaultOn = "network".equals(kind) || "headset".equals(kind);
        return prefs(c).getBoolean(kind, defaultOn);
    }

    public static void setEnabled(Context c, String kind, boolean on) {
        prefs(c).edit().putBoolean(kind, on).apply();
    }

    /** Which sensors this phone really has (names only, no readings). */
    public static Map<String, Object> available(Context c) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        SensorManager sm = (SensorManager) c.getSystemService(Context.SENSOR_SERVICE);
        if (sm == null) return out;
        out.put("accelerometer", sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER) != null);
        out.put("gyroscope", sm.getDefaultSensor(Sensor.TYPE_GYROSCOPE) != null);
        out.put("proximity", sm.getDefaultSensor(Sensor.TYPE_PROXIMITY) != null);
        out.put("light", sm.getDefaultSensor(Sensor.TYPE_LIGHT) != null);
        out.put("magnetometer", sm.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD) != null);
        return out;
    }

    /**
     * The context to send with a check-in: only the kinds you switched on. ``mayBlock`` is false on the main thread, where
     * the (short, waiting) motion and light samples are skipped.
     */
    public static Map<String, Object> context(Context c, boolean mayBlock) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        try {
            if (enabled(c, "network")) out.put("network", network(c));
            if (enabled(c, "headset")) out.put("headset", headset(c));
            if (enabled(c, "orientation")) {
                out.put("orientation", c.getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE
                        ? "landscape" : "portrait");
            }
            if (mayBlock && enabled(c, "motion")) out.put("motion", motion(c));
            if (mayBlock && enabled(c, "light")) out.put("light", light(c));
        } catch (Exception ignored) {
            // a failing sensor must never break the check-in: the PC treats missing values as unknown
        }
        return out;
    }

    static String network(Context c) {
        ConnectivityManager cm = (ConnectivityManager) c.getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return "unknown";
        Network n = cm.getActiveNetwork();
        if (n == null) return "none";
        NetworkCapabilities caps = cm.getNetworkCapabilities(n);
        if (caps == null) return "none";
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return "wifi";
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) return "cellular";
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) return "ethernet";
        return "other";
    }

    static boolean headset(Context c) {
        AudioManager am = (AudioManager) c.getSystemService(Context.AUDIO_SERVICE);
        if (am == null) return false;
        for (AudioDeviceInfo d : am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
            int t = d.getType();
            if (t == AudioDeviceInfo.TYPE_WIRED_HEADPHONES || t == AudioDeviceInfo.TYPE_WIRED_HEADSET
                    || t == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP || t == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                    || t == AudioDeviceInfo.TYPE_USB_HEADSET) return true;
        }
        return false;
    }

    /** "stationary", "moving" or "unknown" (no accelerometer, or too few readings). */
    static String motion(Context c) {
        List<float[]> samples = sample(c, Sensor.TYPE_ACCELEROMETER, 30, 1500);
        if (samples.size() < 10) return "unknown";
        double[] mag = new double[samples.size()];
        double mean = 0;
        for (int i = 0; i < mag.length; i++) {
            float[] v = samples.get(i);
            mag[i] = Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
            mean += mag[i];
        }
        mean /= mag.length;
        double var = 0;
        for (double m : mag) var += (m - mean) * (m - mean);
        double sd = Math.sqrt(var / mag.length);
        return sd < 0.25 ? "stationary" : "moving";       // starting thresholds, not a validated activity classifier
    }

    /** "dark", "dim", "bright" or "unknown". */
    static String light(Context c) {
        List<float[]> samples = sample(c, Sensor.TYPE_LIGHT, 1, 1000);
        if (samples.isEmpty()) return "unknown";
        float lux = samples.get(0)[0];
        return lux < 10 ? "dark" : lux < 200 ? "dim" : "bright";
    }

    private static List<float[]> sample(Context c, int type, final int count, long maxMillis) {
        final List<float[]> got = Collections.synchronizedList(new ArrayList<float[]>());
        SensorManager sm = (SensorManager) c.getSystemService(Context.SENSOR_SERVICE);
        Sensor sensor = sm == null ? null : sm.getDefaultSensor(type);
        if (sensor == null) return got;
        final CountDownLatch done = new CountDownLatch(1);
        HandlerThread thread = new HandlerThread("jarvis-sensor");
        thread.start();
        SensorEventListener listener = new SensorEventListener() {
            @Override public void onSensorChanged(SensorEvent event) {
                got.add(event.values.clone());
                if (got.size() >= count) done.countDown();
            }
            @Override public void onAccuracyChanged(Sensor s, int accuracy) {}
        };
        try {
            sm.registerListener(listener, sensor, SensorManager.SENSOR_DELAY_GAME, new Handler(thread.getLooper()));
            done.await(maxMillis, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            sm.unregisterListener(listener);
            thread.quitSafely();
        }
        return new ArrayList<float[]>(got);
    }

    /** True when called from a worker thread (blocking samples are allowed). */
    static boolean mayBlock() {
        return Looper.myLooper() != Looper.getMainLooper();
    }
}
