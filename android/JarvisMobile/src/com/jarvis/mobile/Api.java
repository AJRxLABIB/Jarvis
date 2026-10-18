package com.jarvis.mobile;

import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URL;
import java.security.MessageDigest;
import java.security.cert.X509Certificate;
import java.util.Map;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.X509TrustManager;

/**
 * HTTPS to one PC, pinned to that PC's certificate fingerprint (the one shown in the QR at pairing). The PC's
 * certificate is self-signed for its home-network address, so instead of the usual CA check we accept exactly
 * the pinned certificate and nothing else - a stranger on the network can't impersonate the PC. If the PC's
 * certificate ever changes (e.g. it was reinstalled), the pin won't match and the app refuses to connect until
 * you pair again, rather than trusting silently.
 */
public final class Api {
    public static final class Result {
        public final int status;
        public final String body;
        public Result(int status, String body) { this.status = status; this.body = body; }
        public Map<String, Object> json() { return Json.object(body); }
        public boolean ok() { return status >= 200 && status < 300 && Json.bool(json(), "ok"); }
    }

    private Api() {}

    private static SSLSocketFactory pinnedFactory(final String pinnedFingerprint) throws Exception {
        final String pin = pinnedFingerprint == null ? "" : pinnedFingerprint.replace(":", "").toUpperCase();
        X509TrustManager tm = new X509TrustManager() {
            @Override public void checkClientTrusted(X509Certificate[] chain, String authType) {}
            @Override public void checkServerTrusted(X509Certificate[] chain, String authType)
                    throws java.security.cert.CertificateException {
                if (pin.isEmpty()) throw new java.security.cert.CertificateException("no pinned certificate");
                if (chain == null || chain.length == 0) throw new java.security.cert.CertificateException("no certificate");
                try {
                    byte[] der = chain[0].getEncoded();
                    String actual = hex(MessageDigest.getInstance("SHA-256").digest(der));
                    if (!constantTimeEquals(actual, pin)) {
                        throw new java.security.cert.CertificateException("certificate does not match the paired PC");
                    }
                } catch (java.security.cert.CertificateException e) {
                    throw e;
                } catch (Exception e) {
                    throw new java.security.cert.CertificateException(e);
                }
            }
            @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
        };
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, new javax.net.ssl.TrustManager[]{tm}, new java.security.SecureRandom());
        return ctx.getSocketFactory();
    }

    private static boolean constantTimeEquals(String a, String b) {
        if (a.length() != b.length()) return false;
        int r = 0;
        for (int i = 0; i < a.length(); i++) r |= a.charAt(i) ^ b.charAt(i);
        return r == 0;
    }

    private static String hex(byte[] bytes) {
        StringBuilder b = new StringBuilder();
        for (byte x : bytes) b.append(String.format("%02X", x));
        return b.toString();
    }

    public static Result post(Pc pc, boolean away, String path, String jsonBody) throws Exception {
        return post(pc, away, path, jsonBody, 20000);
    }

    public static Result post(Pc pc, boolean away, String path, String jsonBody, int readTimeoutMs) throws Exception {
        URL url = new URL(pc.baseUrl(away) + path);
        HttpsURLConnection conn = (HttpsURLConnection) url.openConnection();
        conn.setSSLSocketFactory(pinnedFactory(pc.certFingerprint));
        conn.setHostnameVerifier((hostname, session) -> true); // pinning already proves it's the right PC
        conn.setRequestMethod("POST");
        conn.setConnectTimeout(6000);
        conn.setReadTimeout(readTimeoutMs);
        conn.setDoOutput(true);
        conn.setRequestProperty("Content-Type", "application/json");
        byte[] payload = jsonBody.getBytes("UTF-8");
        OutputStream out = conn.getOutputStream();
        out.write(payload);
        out.close();
        int status = conn.getResponseCode();
        InputStream in = status >= 400 ? conn.getErrorStream() : conn.getInputStream();
        String body = read(in);
        conn.disconnect();
        return new Result(status, body);
    }

    /** Send a signed command; returns the parsed result. Throws on network/TLS problems. */
    public static Result command(Pc pc, boolean away, String deviceId, String command, Map<String, Object> args)
            throws Exception {
        return post(pc, away, "/api/mobile/command", Protocol.envelope(deviceId, command, args));
    }

    /** A signed command the PC may hold open (``wait_events``): a longer read timeout. */
    public static Result command(Pc pc, boolean away, String deviceId, String command, Map<String, Object> args,
                                 int readTimeoutMs) throws Exception {
        return post(pc, away, "/api/mobile/command", Protocol.envelope(deviceId, command, args), readTimeoutMs);
    }

    private static String read(InputStream in) throws Exception {
        if (in == null) return "";
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        byte[] chunk = new byte[4096];
        int n;
        while ((n = in.read(chunk)) != -1) buffer.write(chunk, 0, n);
        in.close();
        return new String(buffer.toByteArray(), "UTF-8");
    }
}
