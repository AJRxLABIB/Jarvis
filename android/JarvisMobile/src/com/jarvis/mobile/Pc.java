package com.jarvis.mobile;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** One paired computer: how to reach it, its pinned certificate, and Wake-on-LAN details. Stored as JSON. */
public final class Pc {
    public String id = "";          // the device_id the PC gave at pairing
    public String name = "PC";
    public String host = "";        // LAN address / hostname
    public int port = 8765;
    public String certFingerprint = ""; // SHA-256 of the PC's TLS cert (pinned)
    public String awayHost = "";    // Tailscale address, if any
    // Wake-on-LAN
    public String mac = "";
    public String broadcast = "";
    public String publicAddress = "";
    public int publicPort = 9;
    public boolean authKeyRegistered = false;

    public String baseUrl(boolean away) {
        String h = away && !awayHost.isEmpty() ? awayHost : host;
        return "https://" + h + ":" + port;
    }

    public List<String> wakeTargets() {
        List<String> targets = new ArrayList<String>();
        if (!broadcast.isEmpty()) for (String t : broadcast.split("[,\\s]+")) if (!t.trim().isEmpty()) targets.add(t.trim());
        if (targets.isEmpty() && !host.isEmpty()) targets.add("255.255.255.255");
        if (!publicAddress.isEmpty()) targets.add(publicAddress + ":" + publicPort);
        return targets;
    }

    public JSONObject toJson() throws Exception {
        JSONObject o = new JSONObject();
        o.put("id", id).put("name", name).put("host", host).put("port", port)
                .put("cert", certFingerprint).put("away", awayHost).put("mac", mac).put("broadcast", broadcast)
                .put("public", publicAddress).put("public_port", publicPort).put("auth_registered", authKeyRegistered);
        return o;
    }

    public static Pc fromJson(JSONObject o) {
        Pc pc = new Pc();
        pc.id = o.optString("id");
        pc.name = o.optString("name", "PC");
        pc.host = o.optString("host");
        pc.port = o.optInt("port", 8765);
        pc.certFingerprint = o.optString("cert");
        pc.awayHost = o.optString("away");
        pc.mac = o.optString("mac");
        pc.broadcast = o.optString("broadcast");
        pc.publicAddress = o.optString("public");
        pc.publicPort = o.optInt("public_port", 9);
        pc.authKeyRegistered = o.optBoolean("auth_registered");
        return pc;
    }

    public static String listToJson(List<Pc> pcs) {
        try {
            JSONArray a = new JSONArray();
            for (Pc pc : pcs) a.put(pc.toJson());
            return a.toString();
        } catch (Exception e) {
            return "[]";
        }
    }

    public static List<Pc> listFromJson(String text) {
        List<Pc> pcs = new ArrayList<Pc>();
        try {
            JSONArray a = new JSONArray(text == null || text.isEmpty() ? "[]" : text);
            for (int i = 0; i < a.length(); i++) pcs.add(fromJson(a.getJSONObject(i)));
        } catch (Exception ignored) {
        }
        return pcs;
    }
}
