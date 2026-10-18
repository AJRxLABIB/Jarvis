package com.jarvis.mobile;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Toast;

import java.util.LinkedHashMap;
import java.util.Map;

/** "Share > JARVIS Mobile" for text or a link: sends it to the paired PC (it shows up in the PC's notifications). */
public final class ShareActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        Intent intent = getIntent();
        final String text = intent == null ? null : intent.getStringExtra(Intent.EXTRA_TEXT);
        final Pc pc = new Store(this).current();
        if (text == null || text.trim().isEmpty() || pc == null) {
            Toast.makeText(this, pc == null ? "Pair a PC first." : "Nothing to share.", Toast.LENGTH_SHORT).show();
            finish();
            return;
        }
        final Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("text", text.length() > 2000 ? text.substring(0, 2000) : text);
        new Thread(new Runnable() {
            @Override public void run() {
                boolean ok;
                try {
                    ok = Api.command(pc, false, pc.id, "phone_share", args).ok();
                } catch (Exception e) {
                    try { ok = Api.command(pc, true, pc.id, "phone_share", args).ok(); } catch (Exception again) { ok = false; }
                }
                final boolean sent = ok;
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        Toast.makeText(ShareActivity.this, sent ? "Sent to your PC." : "Couldn't reach your PC.",
                                Toast.LENGTH_SHORT).show();
                        finish();
                    }
                });
            }
        }).start();
    }
}
