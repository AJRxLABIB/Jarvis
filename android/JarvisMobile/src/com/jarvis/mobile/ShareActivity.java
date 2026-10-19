package com.jarvis.mobile;

import android.app.Activity;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.util.Base64;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * "Share > JARVIS Mobile":
 * - text or a link -> the PC's notifications (as before);
 * - a photo / any file -> saved on the PC in data\phone_inbox (signed upload, up to 25 MB, never run there).
 */
public final class ShareActivity extends Activity {
    static final int MAX_BYTES = 25 * 1024 * 1024;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        Intent intent = getIntent();
        final Pc pc = new Store(this).current();
        if (pc == null) {
            done("Pair a PC first.");
            return;
        }
        final Uri stream = intent == null ? null : (Uri) intent.getParcelableExtra(Intent.EXTRA_STREAM);
        final String text = intent == null ? null : intent.getStringExtra(Intent.EXTRA_TEXT);
        if (stream != null) {
            sendFile(pc, stream);
            return;
        }
        if (text == null || text.trim().isEmpty()) {
            done("Nothing to share.");
            return;
        }
        final Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("text", text.length() > 2000 ? text.substring(0, 2000) : text);
        new Thread(() -> {
            boolean ok;
            try {
                ok = Api.command(pc, false, pc.id, "phone_share", args).ok();
            } catch (Exception e) {
                try { ok = Api.command(pc, true, pc.id, "phone_share", args).ok(); } catch (Exception again) { ok = false; }
            }
            final boolean sent = ok;
            runOnUiThread(() -> done(sent ? "Sent to your PC." : "Couldn't reach your PC."));
        }).start();
    }

    private void sendFile(final Pc pc, final Uri uri) {
        Toast.makeText(this, "Sending to your PC...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            String message;
            try {
                byte[] data = read(uri);
                if (data == null) {
                    message = "That file is bigger than 25 MB.";
                } else {
                    Map<String, Object> args = new LinkedHashMap<String, Object>();
                    args.put("name", name(uri));
                    args.put("data", Base64.encodeToString(data, Base64.NO_WRAP));
                    String body = Protocol.envelope(pc.id, "phone_file", args);
                    Api.Result result;
                    try {
                        result = Api.post(pc, false, "/api/mobile/upload", body, 120000);
                    } catch (Exception e) {
                        result = Api.post(pc, true, "/api/mobile/upload", Protocol.envelope(pc.id, "phone_file", args), 120000);
                    }
                    message = result.ok() ? Json.str(result.json(), "message", "Sent to your PC.")
                            : "Your PC said no: " + Json.str(result.json(), "message", "error " + result.status);
                }
            } catch (Exception e) {
                message = "Couldn't send it: " + e.getMessage();
            }
            final String shown = message;
            runOnUiThread(() -> done(shown));
        }).start();
    }

    private byte[] read(Uri uri) throws Exception {
        InputStream in = getContentResolver().openInputStream(uri);
        if (in == null) throw new java.io.IOException("can't open it");
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[64 * 1024];
        int n;
        try {
            while ((n = in.read(buf)) != -1) {
                out.write(buf, 0, n);
                if (out.size() > MAX_BYTES) return null;
            }
        } finally {
            in.close();
        }
        return out.toByteArray();
    }

    private String name(Uri uri) {
        Cursor cur = getContentResolver().query(uri, new String[]{"_display_name"}, null, null, null);
        if (cur != null) {
            try {
                if (cur.moveToFirst() && cur.getString(0) != null) return cur.getString(0);
            } finally {
                cur.close();
            }
        }
        String last = uri.getLastPathSegment();
        return last == null ? "shared-file" : last;
    }

    private void done(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
        finish();
    }
}
