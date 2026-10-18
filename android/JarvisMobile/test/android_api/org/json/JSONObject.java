package org.json;
public class JSONObject { public JSONObject() {} public JSONObject put(String k, Object v) { return this; }
    public String optString(String k) { return ""; } public String optString(String k, String d) { return d; }
    public int optInt(String k, int d) { return d; } public boolean optBoolean(String k) { return false; } }
