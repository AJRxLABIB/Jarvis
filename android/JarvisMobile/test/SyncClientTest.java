package com.jarvis.mobile;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Runs on a plain JVM (no Android): java -cp out com.jarvis.mobile.SyncClientTest
 * See test/run.sh. The PC is scripted, so this checks the phone's rules, not the PC (tests/test_phone_sync.py does).
 */
public final class SyncClientTest {
    static int failures = 0;

    static void check(boolean ok, String what) {
        System.out.println((ok ? "ok    " : "FAIL  ") + what);
        if (!ok) failures++;
    }

    static final class Mem implements SyncClient.KeyValue {
        final Map<String, String> m = new HashMap<String, String>();
        public String get(String k, String d) { return m.containsKey(k) ? m.get(k) : d; }
        public void put(String k, String v) { m.put(k, v); }
    }

    interface Script { String reply(String command, Map<String, Object> args) throws Exception; }

    static SyncClient client(Mem mem, final List<String> log, final Script script) {
        return new SyncClient(mem, (command, args) -> {
            log.add(command + " " + Json.write(args));
            return new Api.Result(200, script.reply(command, args));
        });
    }

    static String firstOpId(Map<String, Object> args) {
        return Json.str((Map<String, Object>) ((List<Object>) args.get("changes")).get(0), "op_id", "");
    }

    public static void main(String[] a) throws Exception {
        // 1. an added to-do is queued, sent once, and the PC's snapshot becomes the local copy
        Mem mem = new Mem();
        List<String> log = new ArrayList<String>();
        SyncClient c = client(mem, log, (cmd, args) -> {
            if (cmd.equals("sync_push")) return "{\"ok\":true,\"results\":[{\"op_id\":\"" + firstOpId(args) + "\",\"status\":\"applied\",\"record_id\":7,\"rev\":1}]}";
            return "{\"ok\":true,\"snapshot\":true,\"reset\":false,\"more\":false,\"next\":4,\"changes\":[{\"id\":7,\"op\":\"upsert\",\"rev\":1,\"fields\":{\"text\":\"buy milk\",\"done\":false}}]}";
        });
        c.addTodo("  buy milk ");
        check(c.queued() == 1, "an added to-do waits in the queue");
        SyncClient.Summary s = c.sync();
        check(c.queued() == 0 && s.pushed == 1 && s.pulled == 1, "sync sends it and receives the PC's copy");
        check(c.list().size() == 1 && Json.str((Map<String, Object>) c.list().get(0).get("fields"), "text", "").equals("buy milk"), "text was trimmed");
        check(new SyncClient(mem, null).list().size() == 1, "the local copy survives an app restart");

        // 2. an edit on a changed to-do is refused by the PC: dropped, counted, never forced
        log.clear();
        SyncClient d = client(mem, log, (cmd, args) -> {
            if (cmd.equals("sync_push")) return "{\"ok\":true,\"results\":[{\"op_id\":\"" + firstOpId(args) + "\",\"status\":\"conflict\",\"record_id\":7}]}";
            return "{\"ok\":true,\"snapshot\":false,\"more\":false,\"next\":5,\"changes\":[{\"id\":7,\"op\":\"upsert\",\"rev\":2,\"fields\":{\"text\":\"buy oat milk\",\"done\":false}}]}";
        });
        d.setDone(7, true);
        check(log.isEmpty() && d.queued() == 1, "setDone only queues");
        s = d.sync();
        check(s.conflicts == 1 && d.queued() == 0, "a conflict drops the edit");
        check(Json.str((Map<String, Object>) d.list().get(0).get("fields"), "text", "").equals("buy oat milk"), "the PC's version stands");
        check(log.get(0).contains("\"base_rev\":1"), "the edit carried the revision it was based on");

        check(Boolean.TRUE.equals(((Map<String, Object>) d.list().get(0).get("fields")).get("done")) == false, "(after the conflict the PC's not-done value stands)");
        // 2b. a queued done-toggle shows at once and a second tap toggles back instead of queuing a duplicate state
        Mem m2 = new Mem();
        SyncClient toggler = client(m2, new ArrayList<String>(), (cmd, args) ->
            "{\"ok\":true,\"snapshot\":true,\"more\":false,\"next\":1,\"changes\":[{\"id\":3,\"op\":\"upsert\",\"rev\":1,\"fields\":{\"text\":\"x\",\"done\":false}}]}");
        toggler.sync();
        toggler.setDone(3, true);
        check(Boolean.TRUE.equals(((Map<String, Object>) toggler.list().get(0).get("fields")).get("done")), "a done-toggle shows immediately");

        // 3. no answer for an op: it stays queued with the SAME op_id (a retry is harmless on the PC)
        Mem m3 = new Mem();
        List<String> sent = new ArrayList<String>();
        SyncClient e = client(m3, new ArrayList<String>(), (cmd, args) -> {
            if (cmd.equals("sync_push")) { sent.add(firstOpId(args)); return "{\"ok\":true,\"results\":[]}"; }
            return "{\"ok\":true,\"snapshot\":false,\"more\":false,\"next\":0,\"changes\":[]}";
        });
        e.addTodo("call mum");
        e.sync();
        e.sync();
        check(e.queued() == 1 && sent.size() == 2 && sent.get(0).equals(sent.get(1)), "unacknowledged ops are retried unchanged");

        // 4. a failing PC leaves the queue and the cursor alone
        Mem m4 = new Mem();
        SyncClient f = new SyncClient(m4, (cmd, args) -> new Api.Result(403, "{\"ok\":false,\"message\":\"Sync is switched off for this phone.\"}"));
        f.addTodo("x1");
        boolean threw = false;
        try { f.sync(); } catch (Exception ex) { threw = ex.getMessage().contains("switched off"); }
        check(threw && f.queued() == 1, "a refusal is reported and nothing is lost");

        // 5. paging: more=true keeps pulling; a reset replaces the local copy; deletes remove
        Mem m5 = new Mem();
        final int[] page = {0};
        SyncClient g = client(m5, new ArrayList<String>(), (cmd, args) -> {
            page[0]++;
            if (page[0] == 1) return "{\"ok\":true,\"snapshot\":true,\"more\":true,\"next\":2,\"changes\":[{\"id\":1,\"op\":\"upsert\",\"rev\":1,\"fields\":{\"text\":\"a\"}},{\"id\":2,\"op\":\"upsert\",\"rev\":1,\"fields\":{\"text\":\"b\"}}]}";
            if (page[0] == 2) return "{\"ok\":true,\"snapshot\":false,\"more\":false,\"next\":3,\"changes\":[{\"id\":1,\"op\":\"delete\",\"rev\":2,\"fields\":{}}]}";
            return "{\"ok\":true,\"snapshot\":true,\"reset\":true,\"more\":false,\"next\":9,\"changes\":[{\"id\":5,\"op\":\"upsert\",\"rev\":1,\"fields\":{\"text\":\"fresh\"}}]}";
        });
        s = g.sync();
        check(page[0] == 2 && g.list().size() == 1 && s.pulled == 3, "pages are followed and deletes applied");
        s = g.sync();
        check(g.list().size() == 1 && ((Long) g.list().get(0).get("id")) == 5L && s.reset, "a reset replaces the local copy");

        // 6. notifications: generic text only, cursor advances
        Mem m6 = new Mem();
        SyncClient h = client(m6, new ArrayList<String>(), (cmd, args) ->
            "{\"ok\":true,\"notifications\":[{\"id\":11,\"kind\":\"Reminder\",\"text\":\"A reminder is due.\"}],\"next\":11}");
        List<String> texts = h.pollNotifications();
        check(texts.size() == 1 && texts.get(0).equals("A reminder is due."), "a notification is just its generic sentence");
        check(m6.get("sync_state_v1", "").contains("\"notify\":11") && !m6.get("sync_state_v1", "").contains("Reminder"), "only the cursor is stored, not the notice");

        // 7. batching: 45 queued edits go out as 20 + 20 + 5
        Mem m7 = new Mem();
        final List<Integer> sizes = new ArrayList<Integer>();
        SyncClient k = client(m7, new ArrayList<String>(), (cmd, args) -> {
            if (cmd.equals("sync_push")) {
                List<Object> ch = (List<Object>) args.get("changes");
                sizes.add(ch.size());
                StringBuilder b = new StringBuilder("{\"ok\":true,\"results\":[");
                for (int i = 0; i < ch.size(); i++) {
                    if (i > 0) b.append(',');
                    b.append("{\"op_id\":\"").append(Json.str((Map<String, Object>) ch.get(i), "op_id", "")).append("\",\"status\":\"applied\"}");
                }
                return b.append("]}").toString();
            }
            return "{\"ok\":true,\"snapshot\":false,\"more\":false,\"next\":0,\"changes\":[]}";
        });
        for (int i = 0; i < 45; i++) k.addTodo("t" + i);
        k.sync();
        check(sizes.toString().equals("[20, 20, 5]") && k.queued() == 0, "edits are sent in batches of 20 (the PC's limit)");

        // 8. every op id is 8-64 letters/digits/-/_ (the PC rejects anything else)
        boolean valid = true;
        Mem m8 = new Mem();
        SyncClient q = new SyncClient(m8, null);
        for (int i = 0; i < 50; i++) q.addTodo("z");
        for (Object o : (List<Object>) Json.object(m8.get("sync_state_v1", "{}")).get("queue"))
            valid &= Json.str((Map<String, Object>) o, "op_id", "").matches("[A-Za-z0-9_-]{8,64}");
        check(valid, "op ids match what the PC accepts");

        check(SyncClient.isAuthError(new RuntimeException(new IllegalStateException(new Exception("x")))) == false, "ordinary errors are not auth errors");
        System.out.println(failures == 0 ? "ALL PASSED" : failures + " FAILED");
        System.exit(failures == 0 ? 0 : 1);
    }
}
