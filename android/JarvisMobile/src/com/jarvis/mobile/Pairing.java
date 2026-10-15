package com.jarvis.mobile;

import android.net.Uri;
import android.os.Build;

import java.util.Map;

/**
 * Turns a jarvismobile://pair (or jarviswake://pair) deep link - opened from the PC's QR page - into a paired,
 * trusted-pending PC: makes the keys, sends the public key with proof of the one-time secret, stores the PC with
 * its pinned certificate, then enrols the biometric key. The user still presses Trust on the PC.
 */
public final class Pairing {
    public static final class Outcome {
        public final boolean ok;
        public final String message;
        public final Pc pc;
        public Outcome(boolean ok, String message, Pc pc) { this.ok = ok; this.message = message; this.pc = pc; }
    }

    private Pairing() {}

    public static boolean isPairLink(Uri uri) {
        return uri != null && ("jarvismobile".equals(uri.getScheme()) || "jarviswake".equals(uri.getScheme()))
                && "pair".equals(uri.getHost());
    }

    /** Runs on a background thread. */
    public static Outcome pair(Store store, Uri uri, String appVersion) {
        try {
            String host = uri.getQueryParameter("host");
            String portStr = uri.getQueryParameter("port");
            String pid = uri.getQueryParameter("pid");
            String secret = uri.getQueryParameter("s");
            String fp = uri.getQueryParameter("fp");
            String pcName = uri.getQueryParameter("pc");
            if (host == null || pid == null || secret == null || fp == null || fp.isEmpty()) {
                return new Outcome(false, "That pairing link is incomplete. Start pairing again on the PC.", null);
            }
            Pc pc = new Pc();
            pc.host = host;
            pc.port = portStr == null ? 8765 : Integer.parseInt(portStr);
            pc.certFingerprint = fp;
            pc.name = pcName == null || pcName.isEmpty() ? host : pcName;

            Crypto.ensureBaseKey();
            boolean authOk = Crypto.ensureAuthKey();
            String name = Build.MANUFACTURER + " " + Build.MODEL;
            String body = Protocol.pairingBody(pid, secret, name, "android",
                    "Android " + Build.VERSION.RELEASE, appVersion, isHardwareBacked(), authOk);
            Api.Result result = Api.post(pc, false, "/api/mobile/pair", body);
            Map<String, Object> json = result.json();
            if (!result.ok()) {
                return new Outcome(false, Json.str(json, "message", "Pairing was refused (" + result.status + ")."), null);
            }
            pc.id = Json.str(json, "device_id", "");
            store.upsert(pc);

            if (authOk) {
                try {
                    Api.Result reg = Api.command(pc, false, pc.id, "register_auth_key",
                            Protocol.registerAuthArgs(pc.id));
                    if (reg.ok()) {
                        pc.authKeyRegistered = true;
                        store.upsert(pc);
                    }
                } catch (Exception ignored) {
                    // enrolment can be finished later from Settings; pairing itself succeeded
                }
            }
            fetchWakeConfig(store, pc);  // fill the Wake-on-LAN details so the Wake button works
            String note = "Paired. On the PC, check the fingerprint matches and press Trust:\n"
                    + Json.str(json, "fingerprint", "");
            if (!authOk) {
                note += "\n\nSet a screen lock (fingerprint / PIN) on this phone to control the PC - it protects the "
                        + "control key.";
            }
            return new Outcome(true, note, pc);
        } catch (Exception e) {
            return new Outcome(false, "Pairing failed: " + e.getMessage(), null);
        }
    }

    /** Ask the PC for its Wake-on-LAN details (MAC, broadcast) and store them, so the Wake button activates.
     * Safe to call any time; ignores failures (e.g. before Trust). */
    public static void fetchWakeConfig(Store store, Pc pc) {
        try {
            Api.Result r = Api.command(pc, false, pc.id, "wake_config", new java.util.LinkedHashMap<String, Object>());
            java.util.Map<String, Object> j = r.json();
            if (r.ok()) {
                pc.mac = Json.str(j, "mac", pc.mac);
                pc.broadcast = Json.str(j, "broadcast", pc.broadcast);
                String pub = Json.str(j, "public", "");
                if (!pub.isEmpty()) pc.publicAddress = pub;
                String pport = Json.str(j, "public_port", "");
                if (!pport.isEmpty()) { try { pc.publicPort = Integer.parseInt(pport); } catch (Exception ignored) {} }
                store.upsert(pc);
            }
        } catch (Exception ignored) {
        }
    }

    private static boolean isHardwareBacked() {
        // A best-effort hint only; the PC never trusts this for security.
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P;
    }
}
