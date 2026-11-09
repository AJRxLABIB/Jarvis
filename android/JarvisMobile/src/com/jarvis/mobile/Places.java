package com.jarvis.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationManager;
import android.os.Build;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Place alerts: the PC sends this phone a short list of places (name, centre, radius); the phone decides ITSELF whether you arrived, left or stayed, and tells the PC
 * only the event - never a stream of coordinates. Same rules as the PC's core/geofence.py: a position whose accuracy is worse than 100 m (or than the place's radius)
 * says nothing; the first good fix only learns where you are; arriving or leaving needs two good fixes in a row (leaving also needs a margin beyond the edge); the same
 * kind of event for one place is not repeated within 5 minutes; staying 10 minutes gives one "dwell".
 *
 * HOW OFTEN: the phone looks at the last known / a fresh network location about every 15 minutes while "Place alerts" is on (the existing background job), and whenever
 * the app is opened. Two good looks are needed, so an arrival can be reported up to about 30 minutes late. That is the price of not draining the battery; this is not
 * a real-time geofence. Android 10+ also needs "Allow all the time" for the background looks.
 *
 * Events are kept until a check-in succeeds (the PC ignores repeats by id). Nothing else about where you are is stored.
 */
public final class Places {
    private Places() {}

    static final double MIN_RADIUS = 50, MAX_RADIUS = 5000, MAX_ACCURACY = 100, EXIT_MARGIN = 0.25;
    static final int MAX_PLACES = 50, CONFIRM = 2, MAX_EVENTS = 40;
    static final long DWELL_MS = 600000L, COOLDOWN_MS = 300000L, MAX_AGE_MS = 15 * 60000L;

    private static SharedPreferences prefs(Context c) { return c.getSharedPreferences("places", Context.MODE_PRIVATE); }

    public static boolean enabled(Context c) { return prefs(c).getBoolean("enabled", false); }

    public static void setEnabled(Context c, boolean on) {
        prefs(c).edit().putBoolean("enabled", on).apply();
        if (on) SyncJob.schedule(c);
    }

    public static boolean hasLocationPermission(Context c) {
        return Build.VERSION.SDK_INT < 23 || c.checkSelfPermission("android.permission.ACCESS_FINE_LOCATION") == PackageManager.PERMISSION_GRANTED;
    }

    public static boolean hasBackgroundPermission(Context c) {
        return Build.VERSION.SDK_INT < 29 || c.checkSelfPermission("android.permission.ACCESS_BACKGROUND_LOCATION") == PackageManager.PERMISSION_GRANTED;
    }

    // ------------------------------------------------------------------ the places the PC sent

    @SuppressWarnings("unchecked")
    public static List<Map<String, Object>> list(Context c) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        Object parsed = Json.parse(prefs(c).getString("list", "[]"));
        if (parsed instanceof List) for (Object o : (List<Object>) parsed) if (o instanceof Map) out.add((Map<String, Object>) o);
        return out;
    }

    private static double num(Object v, double fallback) { return v instanceof Number ? ((Number) v).doubleValue() : fallback; }

    /** Replaces the list after checking every entry; returns how many were accepted. Unknown places lose their state. */
    @SuppressWarnings("unchecked")
    public static int setPlaces(Context c, Object raw) {
        List<Map<String, Object>> ok = new ArrayList<Map<String, Object>>();
        if (raw instanceof List) {
            for (Object o : (List<Object>) raw) {
                if (!(o instanceof Map) || ok.size() >= MAX_PLACES) continue;
                Map<String, Object> p = (Map<String, Object>) o;
                String id = Json.str(p, "id", "");
                double lat = num(p.get("lat"), 999), lon = num(p.get("lon"), 999), r = num(p.get("radius_m"), 0);
                if (!id.matches("[A-Za-z0-9_.:-]{1,64}") || Math.abs(lat) > 90 || Math.abs(lon) > 180 || r < MIN_RADIUS || r > MAX_RADIUS) continue;
                Map<String, Object> clean = new LinkedHashMap<String, Object>();
                clean.put("id", id);
                clean.put("name", Json.str(p, "name", id));
                clean.put("lat", lat);
                clean.put("lon", lon);
                clean.put("radius_m", r);
                ok.add(clean);
            }
        }
        Map<String, Object> state = loadState(c);
        List<String> keep = new ArrayList<String>();
        for (Map<String, Object> p : ok) keep.add(String.valueOf(p.get("id")));
        state.keySet().retainAll(keep);
        prefs(c).edit().putString("list", Json.write(ok)).putString("state", Json.write(state)).apply();
        return ok.size();
    }

    // ------------------------------------------------------------------ the engine (pure: no Android, testable on a JVM)

    static double distanceM(double lat1, double lon1, double lat2, double lon2) {
        double p1 = Math.toRadians(lat1), p2 = Math.toRadians(lat2);
        double a = Math.pow(Math.sin((p2 - p1) / 2), 2) + Math.cos(p1) * Math.cos(p2) * Math.pow(Math.sin(Math.toRadians(lon2 - lon1) / 2), 2);
        return 2 * 6371000.0 * Math.asin(Math.min(1.0, Math.sqrt(a)));
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> slot(Map<String, Object> state, String id) {
        Object s = state.get(id);
        if (s instanceof Map) return (Map<String, Object>) s;
        Map<String, Object> fresh = new LinkedHashMap<String, Object>();
        fresh.put("in", -1.0);          // -1 = not known yet (never assume)
        fresh.put("streak", 0.0);
        fresh.put("since", 0.0);
        fresh.put("dwelled", 0.0);
        state.put(id, fresh);
        return fresh;
    }

    private static Map<String, Object> event(String placeId, String kind, long nowMs, double acc) {
        Map<String, Object> e = new LinkedHashMap<String, Object>();
        e.put("id", "ph-" + nowMs + "-" + kind + "-" + placeId);
        e.put("place", placeId);
        e.put("kind", kind);
        e.put("t", nowMs / 1000.0);
        e.put("accuracy_m", Math.round(acc));
        e.put("sent", false);
        return e;
    }

    /** One position fix -> zero or more events. Mutates ``state``. */
    static List<Map<String, Object>> feed(List<Map<String, Object>> places, Map<String, Object> state, double lat, double lon, double acc, long nowMs) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        for (Map<String, Object> p : places) {
            String id = String.valueOf(p.get("id"));
            double radius = num(p.get("radius_m"), 150);
            if (acc > MAX_ACCURACY || acc > radius) continue;                       // too vague to say anything about this place
            double dist = distanceM(lat, lon, num(p.get("lat"), 0), num(p.get("lon"), 0));
            boolean side;
            if (dist <= radius) side = true;
            else if (dist >= radius * (1 + EXIT_MARGIN) + acc) side = false;
            else { slot(state, id).put("streak", 0.0); continue; }                  // in the margin band: neither in nor out
            Map<String, Object> s = slot(state, id);
            double known = num(s.get("in"), -1);
            if (known < 0) {                                                        // the first good fix only says where we are
                s.put("in", side ? 1.0 : 0.0);
                s.put("since", side ? (double) nowMs : 0.0);
                s.put("streak", 0.0);
                continue;
            }
            boolean inside = known > 0;
            if (inside == side) {
                s.put("streak", 0.0);
                double since = num(s.get("since"), 0);
                if (side && since > 0 && num(s.get("dwelled"), 0) == 0 && nowMs - since >= DWELL_MS) {
                    s.put("dwelled", 1.0);
                    addIfNotCooling(out, s, "dwell", id, nowMs, acc);
                }
                continue;
            }
            double streak = num(s.get("streak"), 0) + 1;
            s.put("streak", streak);
            if (streak >= CONFIRM) {
                s.put("in", side ? 1.0 : 0.0);
                s.put("streak", 0.0);
                s.put("since", side ? (double) nowMs : 0.0);
                s.put("dwelled", 0.0);
                addIfNotCooling(out, s, side ? "enter" : "exit", id, nowMs, acc);
            }
        }
        return out;
    }

    private static void addIfNotCooling(List<Map<String, Object>> out, Map<String, Object> s, String kind, String id, long nowMs, double acc) {
        String key = "last_" + kind;
        double last = num(s.get(key), 0);
        if (last > 0 && nowMs - last < COOLDOWN_MS) return;
        s.put(key, (double) nowMs);
        out.add(event(id, kind, nowMs, acc));
    }

    // ------------------------------------------------------------------ sampling (Android)

    @SuppressWarnings("unchecked")
    private static Map<String, Object> loadState(Context c) {
        Object parsed = Json.parse(prefs(c).getString("state", "{}"));
        return parsed instanceof Map ? (Map<String, Object>) parsed : new LinkedHashMap<String, Object>();
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> loadEvents(Context c) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        Object parsed = Json.parse(prefs(c).getString("events", "[]"));
        if (parsed instanceof List) for (Object o : (List<Object>) parsed) if (o instanceof Map) out.add((Map<String, Object>) o);
        return out;
    }

    private static Location bestLocation(Context c) {
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null) return null;
        Location best = null;
        String[] providers = {LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER, "passive"};
        for (String p : providers) {
            try {
                Location l = lm.getLastKnownLocation(p);
                if (l == null || System.currentTimeMillis() - l.getTime() > MAX_AGE_MS) continue;
                if (best == null || l.getTime() > best.getTime()) best = l;
            } catch (SecurityException ignored) {
                return null;
            } catch (Exception ignored) { /* provider not available */ }
        }
        return best;
    }

    /** Looks at the location once and updates the events. Call from a background thread. Returns a short status for the screen. */
    public static synchronized String sample(Context c) {
        if (!enabled(c)) return "Place alerts are off.";
        List<Map<String, Object>> places = list(c);
        if (places.isEmpty()) return "No places yet: ask JARVIS on the PC to send your places to this phone.";
        if (!hasLocationPermission(c)) return "Location permission is not granted.";
        Location loc = bestLocation(c);
        if (loc == null) return "No recent location (turn Location on; it is read about every 15 minutes).";
        double acc = loc.hasAccuracy() ? loc.getAccuracy() : 9999;
        Map<String, Object> state = loadState(c);
        List<Map<String, Object>> fresh = feed(places, state, loc.getLatitude(), loc.getLongitude(), acc, System.currentTimeMillis());
        List<Map<String, Object>> events = loadEvents(c);
        events.addAll(fresh);
        while (events.size() > MAX_EVENTS) events.remove(0);
        prefs(c).edit().putString("state", Json.write(state)).putString("events", Json.write(events))
                .putLong("last_sample", System.currentTimeMillis()).putString("last_note", acc > MAX_ACCURACY ? "last location too vague (" + Math.round(acc) + " m)" : "ok").apply();
        return fresh.isEmpty() ? "Checked: nothing new (" + (acc > MAX_ACCURACY ? "location too vague" : "no arrival or departure") + ")." : fresh.size() + " new event(s) to report.";
    }

    public static void sampleAsync(final Context c) {
        if (!enabled(c)) return;
        new Thread(new Runnable() { @Override public void run() { try { sample(c); } catch (Exception ignored) { /* next time */ } } }).start();
    }

    // ------------------------------------------------------------------ what goes to the PC

    public static boolean pending(Context c) {
        for (Map<String, Object> e : loadEvents(c)) if (!Boolean.TRUE.equals(e.get("sent"))) return true;
        return false;
    }

    /** Unsent events, without the internal flag, for the check-in report. */
    public static List<Map<String, Object>> eventsForReport(Context c) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        if (!enabled(c)) return out;
        for (Map<String, Object> e : loadEvents(c)) {
            if (Boolean.TRUE.equals(e.get("sent")) || out.size() >= 10) continue;
            Map<String, Object> copy = new LinkedHashMap<String, Object>(e);
            copy.remove("sent");
            out.add(copy);
        }
        return out;
    }

    public static void markSent(Context c, List<Map<String, Object>> reported) {
        if (reported == null || reported.isEmpty()) return;
        List<String> ids = new ArrayList<String>();
        for (Map<String, Object> e : reported) ids.add(String.valueOf(e.get("id")));
        List<Map<String, Object>> events = loadEvents(c);
        for (Map<String, Object> e : events) if (ids.contains(String.valueOf(e.get("id")))) e.put("sent", true);
        prefs(c).edit().putString("events", Json.write(events)).apply();
    }

    public static String summary(Context c) {
        StringBuilder sb = new StringBuilder();
        long last = prefs(c).getLong("last_sample", 0);
        sb.append(enabled(c) ? "On" : "Off").append(", ").append(list(c).size()).append(" place(s). ");
        sb.append(last == 0 ? "Not checked yet." : "Last look " + Math.max(0, (System.currentTimeMillis() - last) / 60000) + " min ago (" + prefs(c).getString("last_note", "") + ").");
        List<Map<String, Object>> events = loadEvents(c);
        for (int i = Math.max(0, events.size() - 5); i < events.size(); i++) {
            Map<String, Object> e = events.get(i);
            sb.append("\n").append(Json.str(e, "kind", "?")).append(" ").append(Json.str(e, "place", "?")).append(Boolean.TRUE.equals(e.get("sent")) ? "  (sent)" : "  (waiting to be sent)");
        }
        return sb.toString();
    }
}
