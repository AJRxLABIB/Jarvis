package android.net.nsd;
public class NsdManager { public static final int PROTOCOL_DNS_SD = 1;
    public interface DiscoveryListener { void onStartDiscoveryFailed(String t, int e); void onStopDiscoveryFailed(String t, int e); void onDiscoveryStarted(String t);
        void onDiscoveryStopped(String t); void onServiceFound(NsdServiceInfo i); void onServiceLost(NsdServiceInfo i); }
    public interface ResolveListener { void onResolveFailed(NsdServiceInfo i, int e); void onServiceResolved(NsdServiceInfo i); }
    public void discoverServices(String t, int p, DiscoveryListener l) {} public void stopServiceDiscovery(DiscoveryListener l) {}
    public void resolveService(NsdServiceInfo i, ResolveListener l) {} }
