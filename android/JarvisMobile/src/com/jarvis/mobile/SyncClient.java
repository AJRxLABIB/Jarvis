package com.jarvis.mobile;

import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The phone's half of to-do sync (PC half: app/remote/sync.py). Pure Java - no Android classes - so its rules can be
 * run on any JVM (android/JarvisMobile/test/SyncClientTest.java does).
 *
 * Rules, mirrored from the PC:
 *  - Nothing syncs unless YOU switched "todos" on for this phone on the PC; the PC answers "not_enabled" otherwise.
 *  - Edits are queued locally with a fixed op_id and sent in batches of at most 20. Sending the same op twice is
 *    harmless (the PC answers "duplicate"), so a timeout or lost answer just retries the same ops.
 *  - An update carries the revision it was based on. If the PC changed that to-do meanwhile, the PC refuses ("conflict"):
 *    this client then drops that edit, keeps the PC's version and reports it - it never overwrites the PC.
 *  - A pull applies the PC's changes in order; a "snapshot" or "reset" replaces the local copy. The cursor only moves
 *    after the changes it covers were stored.
 *  - Methods are synchronized: the screen edits while a background thread syncs.
 *  - Only to-dos and generic notification sentences are ever stored. Nothing is logged.
 */
public final class SyncClient {
    public interface KeyValue {
        String get(String key, String fallback);
        void put(String key, String value);
    }

    public interface Transport {
        Api.Result command(String command, Map<String, Object> arguments) throws Exception;
    }

    /** What one sync did, in plain words. */
    public static final class Summary {
        public int pushed, pulled, conflicts, rejected;
        public boolean reset;
        public final List<String> notes = new ArrayList<String>();

        public String text() {
            StringBuilder b = new StringBuilder();
            b.append("Sent ").append(pushed).append(", received ").append(pulled);
            if (reset) b.append(" (fresh copy from the PC)");
            if (conflicts > 0) b.append("; ").append(conflicts).append(" edit(s) not applied because the PC changed them first");
            if (rejected > 0) b.append("; ").append(rejected).append(" edit(s) refused by the PC");
            return b.append(".").toString();
        }
    }

    static final int BATCH = 20;
    static final int MAX_PULL_ROUNDS = 20;
    private static final String KEY = "sync_state_v1";
    private static final String ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";

    private final KeyValue kv;
    private final Transport transport;
    private final SecureRandom random = new SecureRandom();
    private Map<String, Object> state;

    public SyncClient(KeyValue kv, Transport transport) {
        this.kv = kv;
        this.transport = transport;
        load();
    }

    // ------------------------------------------------------------------ local state

    private void load() {
        state = Json.object(kv.get(KEY, "{}"));
        if (!(state.get("todos") instanceof Map)) state.put("todos", new LinkedHashMap<String, Object>());
        if (!(state.get("queue") instanceof List)) state.put("queue", new ArrayList<Object>());
        if (!(state.get("cursor") instanceof Long)) state.put("cursor", Long.valueOf(0));
        if (!(state.get("notify") instanceof Long)) state.put("notify", Long.valueOf(0));
    }

    private void save() {
        kv.put(KEY, Json.write(state));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> todos() { return (Map<String, Object>) state.get("todos"); }

    @SuppressWarnings("unchecked")
    private List<Object> queue() { return (List<Object>) state.get("queue"); }

    private long num(String key) { return ((Long) state.get(key)).longValue(); }

    public synchronized int queued() { return queue().size(); }

    /** The local copy, oldest first: each map has id, rev and fields (text, due, done, minutes). */
    @SuppressWarnings("unchecked")
    public synchronized List<Map<String, Object>> list() {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        for (Object v : todos().values()) out.add((Map<String, Object>) v);
        return out;
    }

    private String opId() {
        StringBuilder b = new StringBuilder("op");
        for (int i = 0; i < 18; i++) b.append(ID_CHARS.charAt(random.nextInt(ID_CHARS.length())));
        return b.toString();
    }

    // ------------------------------------------------------------------ edits (queued; sent by sync())

    public synchronized void addTodo(String text) {
        Map<String, Object> fields = new LinkedHashMap<String, Object>();
        fields.put("text", text.trim());
        Map<String, Object> op = new LinkedHashMap<String, Object>();
        op.put("op_id", opId());
        op.put("op", "create");
        op.put("fields", fields);
        queue().add(op);
        save();
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> existing(long recordId) {
        Object v = todos().get(String.valueOf(recordId));
        if (v == null) throw new IllegalArgumentException("unknown to-do");
        return (Map<String, Object>) v;
    }

    public synchronized void setDone(long recordId, boolean done) {
        Map<String, Object> local = existing(recordId);
        Map<String, Object> fields = new LinkedHashMap<String, Object>();
        fields.put("done", done);
        queue().add(change("update", recordId, local, fields));
        mapOf(local.get("fields")).put("done", done);   // shown at once; the PC's version replaces it on the next sync
        save();
    }

    public synchronized void deleteTodo(long recordId) {
        queue().add(change("delete", recordId, existing(recordId), null));
        save();
    }

    private Map<String, Object> change(String kind, long recordId, Map<String, Object> local, Map<String, Object> fields) {
        Map<String, Object> op = new LinkedHashMap<String, Object>();
        op.put("op_id", opId());
        op.put("op", kind);
        op.put("record_id", Long.valueOf(recordId));
        op.put("base_rev", local.get("rev"));
        if (fields != null) op.put("fields", fields);
        return op;
    }

    // ------------------------------------------------------------------ sync

    @SuppressWarnings("unchecked")
    private static List<Object> listOf(Object v) { return v instanceof List ? (List<Object>) v : new ArrayList<Object>(); }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> mapOf(Object v) { return v instanceof Map ? (Map<String, Object>) v : new LinkedHashMap<String, Object>(); }

    private Map<String, Object> call(String command, Map<String, Object> args) throws Exception {
        Api.Result r = transport.command(command, args);
        Map<String, Object> body = r.json();
        if (!r.ok()) throw new Exception(Json.str(body, "message", "The PC refused (" + r.status + ")."));
        return body;
    }

    /** Send queued edits, then fetch the PC's changes. Throws on network/refusal; what was done so far is kept. */
    public synchronized Summary sync() throws Exception {
        Summary summary = new Summary();
        push(summary);
        pull(summary);
        return summary;
    }

    /** Fetch the PC's changes only. Used by the background job: sending edits needs the biometric-protected key. */
    public synchronized Summary pullOnly() throws Exception {
        Summary summary = new Summary();
        pull(summary);
        return summary;
    }

    private void push(Summary summary) throws Exception {
        while (!queue().isEmpty()) {
            List<Object> batch = new ArrayList<Object>(queue().subList(0, Math.min(BATCH, queue().size())));
            Map<String, Object> args = new LinkedHashMap<String, Object>();
            args.put("dataset", "todos");
            args.put("changes", batch);
            Map<String, Object> reply = call("sync_push", args);
            Map<String, String> status = new LinkedHashMap<String, String>();
            for (Object o : listOf(reply.get("results"))) {
                Map<String, Object> r = mapOf(o);
                status.put(Json.str(r, "op_id", ""), Json.str(r, "status", ""));
            }
            boolean progress = false;
            for (Object o : batch) {
                String id = Json.str(mapOf(o), "op_id", "");
                String s = status.get(id);
                if (s == null) continue;              // no answer for it: keep it, same op_id, retry next time
                queue().remove(o);
                progress = true;
                if ("applied".equals(s) || "duplicate".equals(s)) summary.pushed++;
                else if ("conflict".equals(s)) { summary.conflicts++; summary.notes.add("conflict: kept the PC's version"); }
                else { summary.rejected++; }
            }
            save();
            if (!progress) break;                     // nothing was acknowledged: stop instead of looping
        }
    }

    private void pull(Summary summary) throws Exception {
        for (int round = 0; round < MAX_PULL_ROUNDS; round++) {
            Map<String, Object> args = new LinkedHashMap<String, Object>();
            args.put("dataset", "todos");
            args.put("since", Long.valueOf(num("cursor")));
            Map<String, Object> reply = call("sync_pull", args);
            if (Json.bool(reply, "snapshot") || Json.bool(reply, "reset")) {
                todos().clear();
                summary.reset = summary.reset || Json.bool(reply, "reset");
            }
            for (Object o : listOf(reply.get("changes"))) {
                Map<String, Object> c = mapOf(o);
                Object id = c.get("id");
                if (!(id instanceof Long)) continue;
                if ("delete".equals(Json.str(c, "op", ""))) {
                    todos().remove(String.valueOf(id));
                } else {
                    Map<String, Object> row = new LinkedHashMap<String, Object>();
                    row.put("id", id);
                    row.put("rev", c.get("rev"));
                    row.put("fields", mapOf(c.get("fields")));
                    todos().put(String.valueOf(id), row);
                }
                summary.pulled++;
            }
            Object next = reply.get("next");
            if (next instanceof Long) state.put("cursor", next);
            save();                                   // the cursor moves only after the changes are stored
            if (!Json.bool(reply, "more")) return;
        }
    }

    // ------------------------------------------------------------------ notifications (generic sentences only)

    public synchronized List<String> pollNotifications() throws Exception {
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("since", Long.valueOf(num("notify")));
        Map<String, Object> reply = call("notifications", args);
        List<String> texts = new ArrayList<String>();
        for (Object o : listOf(reply.get("notifications"))) texts.add(Json.str(mapOf(o), "text", ""));
        Object next = reply.get("next");
        if (next instanceof Long) { state.put("notify", next); save(); }
        return texts;
    }

    public static boolean isAuthError(Throwable e) {
        while (e != null) {
            String n = e.getClass().getName();
            if (n.contains("UserNotAuthenticated") || n.contains("KeyPermanentlyInvalidated")) return true;
            e = e.getCause();
        }
        return false;
    }
}
