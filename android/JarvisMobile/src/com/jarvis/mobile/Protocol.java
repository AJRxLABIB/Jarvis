package com.jarvis.mobile;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Builds the signed command envelope the PC verifies (see app/remote/mobile.py). Signed text is
 * request_id \n device_id \n timestamp \n nonce \n command \n arguments (the exact arguments JSON string).
 *
 * Read-only commands are signed with the base key; commands that change the PC are signed with the auth key,
 * which the Keystore releases only after the user passes fingerprint / face / PIN. STEP_UP mirrors the PC list.
 */
public final class Protocol {
    private Protocol() {}

    public static final java.util.Set<String> STEP_UP = new java.util.HashSet<String>(java.util.Arrays.asList(
            "lock", "sleep", "hibernate", "restart", "shutdown", "wake_device", "volume", "media", "open_app",
            "close_app", "window", "brightness", "open_website", "browser", "put_file", "get_file", "mouse", "key"));

    public static boolean needsAuth(String command) {
        return STEP_UP.contains(command);
    }

    private static String randomId(int bytes) {
        byte[] b = new byte[bytes];
        new java.security.SecureRandom().nextBytes(b);
        return android.util.Base64.encodeToString(b, android.util.Base64.NO_WRAP | android.util.Base64.URL_SAFE
                | android.util.Base64.NO_PADDING);
    }

    /** The envelope as a JSON string ready to POST. ``arguments`` is serialised once and both signed and sent. */
    public static String envelope(String deviceId, String command, Map<String, Object> arguments) throws Exception {
        String argsJson = Json.write(arguments == null ? new LinkedHashMap<String, Object>() : arguments);
        String requestId = randomId(12);
        String nonce = randomId(16);
        long timestamp = System.currentTimeMillis();
        String signed = requestId + "\n" + deviceId + "\n" + timestamp + "\n" + nonce + "\n" + command + "\n" + argsJson;
        String alias = needsAuth(command) ? Crypto.AUTH_ALIAS : Crypto.BASE_ALIAS;
        String signature = Crypto.sign(alias, signed.getBytes("UTF-8"));
        Map<String, Object> env = new LinkedHashMap<String, Object>();
        env.put("request_id", requestId);
        env.put("device_id", deviceId);
        env.put("timestamp", timestamp);
        env.put("nonce", nonce);
        env.put("command", command);
        env.put("arguments", argsJson);
        env.put("signature", signature);
        return Json.write(env);
    }

    /** The pairing request body: proof (HMAC of the one-time secret) + a signature by the new base key. */
    public static String pairingBody(String pairingId, String secret, String name, String platform, String osVersion,
                                     String appVersion, boolean hardware, boolean userAuth) throws Exception {
        String publicKey = Crypto.publicKeyB64(Crypto.BASE_ALIAS);
        byte[] pairingText = (pairingId + "\n" + publicKey + "\n" + name).getBytes("UTF-8");
        javax.crypto.Mac mac = javax.crypto.Mac.getInstance("HmacSHA256");
        mac.init(new javax.crypto.spec.SecretKeySpec(secret.getBytes("UTF-8"), "HmacSHA256"));
        String proof = android.util.Base64.encodeToString(mac.doFinal(pairingText), android.util.Base64.NO_WRAP);
        String signature = Crypto.sign(Crypto.BASE_ALIAS, pairingText);
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("pairing_id", pairingId);
        body.put("public_key", publicKey);
        body.put("name", name);
        body.put("platform", platform);
        body.put("os_version", osVersion);
        body.put("app_version", appVersion);
        Map<String, Object> keyInfo = new LinkedHashMap<String, Object>();
        keyInfo.put("hardware", hardware);
        keyInfo.put("user_auth", userAuth);
        body.put("key_info", keyInfo);
        body.put("proof", proof);
        body.put("signature", signature);
        return Json.write(body);
    }

    /** Enrol the auth key: signed by the auth key over "auth-key\n<deviceId>\n<publicKey>". */
    public static Map<String, Object> registerAuthArgs(String deviceId) throws Exception {
        String authKey = Crypto.publicKeyB64(Crypto.AUTH_ALIAS);
        byte[] proof = ("auth-key\n" + deviceId + "\n" + authKey).getBytes("UTF-8");
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("auth_public_key", authKey);
        args.put("signature", Crypto.sign(Crypto.AUTH_ALIAS, proof));
        return args;
    }
}
