package com.jarvis.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.List;

/** The list of paired computers and the chosen one. Only non-secret data - the keys live in the Keystore. */
public final class Store {
    private final SharedPreferences prefs;

    public Store(Context context) {
        prefs = context.getSharedPreferences("jarvis_mobile", Context.MODE_PRIVATE);
    }

    public List<Pc> pcs() {
        return Pc.listFromJson(prefs.getString("pcs", "[]"));
    }

    public void save(List<Pc> pcs) {
        prefs.edit().putString("pcs", Pc.listToJson(pcs)).apply();
    }

    public void upsert(Pc pc) {
        List<Pc> pcs = pcs();
        boolean found = false;
        for (int i = 0; i < pcs.size(); i++) {
            if (pcs.get(i).id.equals(pc.id) || (!pc.certFingerprint.isEmpty()
                    && pcs.get(i).certFingerprint.equals(pc.certFingerprint))) {
                pcs.set(i, pc);
                found = true;
                break;
            }
        }
        if (!found) pcs.add(pc);
        save(pcs);
        setCurrentId(pc.id);
    }

    public void remove(String id) {
        List<Pc> pcs = pcs();
        for (int i = 0; i < pcs.size(); i++) {
            if (pcs.get(i).id.equals(id)) { pcs.remove(i); break; }
        }
        save(pcs);
    }

    public Pc current() {
        String id = prefs.getString("current", "");
        List<Pc> pcs = pcs();
        for (Pc pc : pcs) if (pc.id.equals(id)) return pc;
        return pcs.isEmpty() ? null : pcs.get(0);
    }

    public void setCurrentId(String id) {
        prefs.edit().putString("current", id).apply();
    }

    public boolean requireBiometricForControl() {
        return prefs.getBoolean("bio_control", true);
    }

    public void setRequireBiometricForControl(boolean on) {
        prefs.edit().putBoolean("bio_control", on).apply();
    }
}
