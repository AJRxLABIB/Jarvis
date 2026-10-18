package android.security.keystore;
public class KeyGenParameterSpec implements java.security.spec.AlgorithmParameterSpec {
    public static class Builder {
        public Builder(String alias, int purposes) {}
        public Builder setAlgorithmParameterSpec(java.security.spec.AlgorithmParameterSpec s) { return this; }
        public Builder setDigests(String... d) { return this; }
        public Builder setUserAuthenticationRequired(boolean b) { return this; }
        public Builder setUserAuthenticationValidityDurationSeconds(int s) { return this; }
        public Builder setUserAuthenticationParameters(int s, int types) { return this; }
        public Builder setInvalidatedByBiometricEnrollment(boolean b) { return this; }
        public Builder setIsStrongBoxBacked(boolean b) { return this; }
        public KeyGenParameterSpec build() { return new KeyGenParameterSpec(); }
    }
}
