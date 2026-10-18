package com.jarvis.mobile;

import java.util.Map;

/** Test stub: only what SyncClient/AppSync use (the real Api needs Android and a PC). Same shape as src/.../Api.java. */
public final class Api {
    public static final class Result {
        public final int status;
        public final String body;
        public Result(int status, String body) { this.status = status; this.body = body; }
        public Map<String, Object> json() { return Json.object(body); }
        public boolean ok() { return status >= 200 && status < 300 && Json.bool(json(), "ok"); }
    }
    public static Result command(Pc pc, boolean away, String deviceId, String command, Map<String, Object> args) throws Exception {
        return new Result(200, "{\"ok\":true}");
    }
    public static Result command(Pc pc, boolean away, String deviceId, String command, Map<String, Object> args,
                                 int readTimeoutMs) throws Exception {
        return command(pc, away, deviceId, command, args);
    }
}
