package com.jarvis.mobile;

import android.content.Context;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Handler;
import android.os.Looper;

import java.net.InetAddress;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Finds JARVIS PCs on the local network (Android NSD / mDNS, service type _jarvis._tcp - what the PC announces when
 * "Local discovery" is on). Finding is never trusting: a result is only a name and an address. Pairing still needs the
 * PC's QR code and your Trust press on the PC, and every later connection is pinned to the PC's certificate, so an
 * impostor that announces the same name cannot get control. Only private-network addresses are accepted, and the
 * text a device announces is shortened and stripped of control characters before it is shown.
 */
public final class Discovery {
    public static final class Found {
        public final String instance, host, id, name, version;
        public final int port;

        Found(String instance, String host, int port, String id, String name, String version) {
            this.instance = instance; this.host = host; this.port = port; this.id = id; this.name = name; this.version = version;
        }
    }

    public interface Listener {
        /** The list so far (called on the main thread each time a PC is resolved). */
        void onFound(List<Found> all);

        /** Discovery ended: note is "" or a short reason it found nothing / could not run. */
        void onDone(String note);
    }

    private static final String TYPE = "_jarvis._tcp.";
    private final NsdManager nsd;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<Found> found = new ArrayList<Found>();
    private final ArrayDeque<NsdServiceInfo> queue = new ArrayDeque<NsdServiceInfo>();
    private NsdManager.DiscoveryListener discovery;
    private Listener listener;
    private boolean resolving = false;
    private boolean running = false;

    public Discovery(Context context) {
        nsd = (NsdManager) context.getApplicationContext().getSystemService(Context.NSD_SERVICE);
    }

    /** Looks for ``millis`` milliseconds, then stops by itself. Safe to call again after it finished. */
    public synchronized void start(Listener l, long millis) {
        if (running) return;
        if (nsd == null) { l.onDone("This phone has no network service discovery."); return; }
        listener = l;
        running = true;
        found.clear();
        queue.clear();
        resolving = false;
        discovery = new NsdManager.DiscoveryListener() {
            @Override public void onDiscoveryStarted(String serviceType) {}
            @Override public void onServiceFound(NsdServiceInfo info) { enqueue(info); }
            @Override public void onServiceLost(NsdServiceInfo info) {}
            @Override public void onDiscoveryStopped(String serviceType) {}
            @Override public void onStartDiscoveryFailed(String serviceType, int errorCode) {
                finish("Discovery could not start (error " + errorCode + "). Wi-Fi must be on; some networks block it.");
            }
            @Override public void onStopDiscoveryFailed(String serviceType, int errorCode) {}
        };
        try {
            nsd.discoverServices(TYPE, NsdManager.PROTOCOL_DNS_SD, discovery);
        } catch (Exception e) {
            finish("Discovery could not start: " + e.getClass().getSimpleName());
            return;
        }
        main.postDelayed(new Runnable() {
            @Override public void run() { finish(found.isEmpty()
                    ? "No JARVIS PC answered. On the PC turn on Local discovery (LOCAL_DISCOVERY), or pair with the QR code." : ""); }
        }, millis);
    }

    public synchronized void stop() { finish(""); }

    private synchronized void finish(final String note) {
        if (!running) return;
        running = false;
        try { if (discovery != null) nsd.stopServiceDiscovery(discovery); } catch (Exception ignored) { /* already stopped */ }
        discovery = null;
        final Listener l = listener;
        if (l != null) main.post(new Runnable() { @Override public void run() { l.onDone(note); } });
    }

    private synchronized void enqueue(NsdServiceInfo info) {
        queue.add(info);
        next();
    }

    /** NSD resolves one service at a time (a second concurrent resolve fails), so they are queued. */
    private synchronized void next() {
        if (resolving || queue.isEmpty() || !running) return;
        resolving = true;
        final NsdServiceInfo info = queue.poll();
        try {
            nsd.resolveService(info, new NsdManager.ResolveListener() {
                @Override public void onResolveFailed(NsdServiceInfo serviceInfo, int errorCode) { resolved(null); }
                @Override public void onServiceResolved(NsdServiceInfo serviceInfo) { resolved(serviceInfo); }
            });
        } catch (Exception e) {
            resolved(null);
        }
    }

    private synchronized void resolved(NsdServiceInfo info) {
        resolving = false;
        if (info != null && running) {
            Found f = convert(info);
            if (f != null && !has(f)) {
                found.add(f);
                final List<Found> copy = new ArrayList<Found>(found);
                final Listener l = listener;
                if (l != null) main.post(new Runnable() { @Override public void run() { l.onFound(copy); } });
            }
        }
        next();
    }

    private boolean has(Found f) {
        for (Found x : found) if (x.host.equals(f.host) && x.port == f.port) return true;
        return false;
    }

    private static Found convert(NsdServiceInfo info) {
        InetAddress address = info.getHost();
        int port = info.getPort();
        if (address == null || port < 1 || port > 65535) return null;
        if (!(address.isSiteLocalAddress() || address.isLinkLocalAddress())) return null;   // your own network only
        Map<String, byte[]> txt = info.getAttributes();
        String id = text(txt, "id"), name = text(txt, "name"), version = text(txt, "v");
        String instance = clean(info.getServiceName());
        return new Found(instance, address.getHostAddress(), port, id, name.isEmpty() ? instance : name, version);
    }

    private static String text(Map<String, byte[]> txt, String key) {
        if (txt == null || !txt.containsKey(key) || txt.get(key) == null) return "";
        try { return clean(new String(txt.get(key), "UTF-8")); } catch (Exception e) { return ""; }
    }

    /** Announced text is untrusted: printable characters only, at most 40. */
    static String clean(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < s.length() && b.length() < 40; i++) {
            char ch = s.charAt(i);
            if (ch >= 32 && ch != 127) b.append(ch);
        }
        return b.toString().trim();
    }

    /** Does this announcement belong to an already paired PC? Its id is the first hex digits of the PC's certificate fingerprint. */
    public static boolean matches(Pc pc, Found f) {
        String fp = pc.certFingerprint == null ? "" : pc.certFingerprint.replace(":", "").toLowerCase();
        return f.id.length() >= 8 && fp.startsWith(f.id.toLowerCase());
    }
}
