package com.jarvis.mobile;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.Signature;

/**
 * The phone's two keys, both in the Android Keystore (hardware-backed where the phone has a secure element).
 * The private keys never leave the phone and are never exported.
 *
 *  - BASE key ("jarvis_device"): identifies the phone. Signs read-only requests and pairing.
 *  - AUTH key ("jarvis_auth"): released by the Keystore only after the user passes fingerprint / face / device
 *    PIN (setUserAuthenticationRequired). Signs everything that changes the PC. This is how "authenticate on the
 *    phone, then act" is real and not just a screen: without a fresh unlock the key won't sign.
 *
 * Neither key is, or can be, a Windows password. Jarvis never handles the Windows password at all.
 */
public final class Crypto {
    public static final String BASE_ALIAS = "jarvis_device";
    public static final String AUTH_ALIAS = "jarvis_auth";
    private static final String KEYSTORE = "AndroidKeyStore";
    private static final int AUTH_VALID_SECONDS = 60; // a fresh unlock covers a short burst of actions

    private Crypto() {}

    private static KeyStore keystore() throws Exception {
        KeyStore ks = KeyStore.getInstance(KEYSTORE);
        ks.load(null);
        return ks;
    }

    public static boolean exists(String alias) {
        try {
            return keystore().containsAlias(alias);
        } catch (Exception e) {
            return false;
        }
    }

    /** Make the base key if it isn't there. Usable without any user authentication (read-only requests). */
    public static void ensureBaseKey() throws Exception {
        if (exists(BASE_ALIAS)) return;
        KeyGenParameterSpec.Builder b = new KeyGenParameterSpec.Builder(BASE_ALIAS,
                KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY)
                .setAlgorithmParameterSpec(new java.security.spec.ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256);
        generate(b.build());
    }

    /**
     * Make the auth key, tied to the device lock (fingerprint / face / PIN). Returns false when the phone has no
     * secure lock set up - then the app tells the user to add one, and won't allow control actions.
     */
    public static boolean ensureAuthKey() throws Exception {
        if (exists(AUTH_ALIAS)) return true;
        KeyGenParameterSpec.Builder b = new KeyGenParameterSpec.Builder(AUTH_ALIAS,
                KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY)
                .setAlgorithmParameterSpec(new java.security.spec.ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(true);
        applyAuthValidity(b);
        try {
            generate(b.build());
            return true;
        } catch (Exception e) {
            return false; // no lock screen / no biometrics enrolled
        }
    }

    @SuppressWarnings("deprecation")
    private static void applyAuthValidity(KeyGenParameterSpec.Builder b) {
        try {
            // API 30+: authenticate with device credential or biometrics
            b.setUserAuthenticationParameters(AUTH_VALID_SECONDS,
                    KeyProperties.AUTH_BIOMETRIC_STRONG | KeyProperties.AUTH_DEVICE_CREDENTIAL);
        } catch (Throwable ignored) {
            b.setUserAuthenticationValidityDurationSeconds(AUTH_VALID_SECONDS);
        }
    }

    private static void generate(KeyGenParameterSpec spec) throws Exception {
        KeyPairGenerator gen = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, KEYSTORE);
        gen.initialize(spec);
        gen.generateKeyPair();
    }

    /** The public key as base64 SubjectPublicKeyInfo DER - exactly what the PC expects. */
    public static String publicKeyB64(String alias) throws Exception {
        PublicKey key = keystore().getCertificate(alias).getPublicKey();
        return Base64.encodeToString(key.getEncoded(), Base64.NO_WRAP);
    }

    public static String fingerprint(String alias) throws Exception {
        byte[] der = keystore().getCertificate(alias).getPublicKey().getEncoded();
        java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
        byte[] hash = md.digest(der);
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < 16; i++) {
            if (i > 0) b.append(':');
            b.append(String.format("%02X", hash[i]));
        }
        return b.toString();
    }

    /** ECDSA-SHA256 over ``message`` with the named key, base64 (DER). Using the auth key may throw
     * UserNotAuthenticatedException, which the caller turns into a biometric prompt then retries. */
    public static String sign(String alias, byte[] message) throws Exception {
        PrivateKey key = (PrivateKey) keystore().getKey(alias, null);
        Signature s = Signature.getInstance("SHA256withECDSA");
        s.initSign(key);
        s.update(message);
        return Base64.encodeToString(s.sign(), Base64.NO_WRAP);
    }

    public static void deleteAll() {
        try {
            KeyStore ks = keystore();
            if (ks.containsAlias(BASE_ALIAS)) ks.deleteEntry(BASE_ALIAS);
            if (ks.containsAlias(AUTH_ALIAS)) ks.deleteEntry(AUTH_ALIAS);
        } catch (Exception ignored) {
        }
    }
}
