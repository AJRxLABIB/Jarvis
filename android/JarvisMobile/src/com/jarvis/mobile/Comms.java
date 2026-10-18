package com.jarvis.mobile;

import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.provider.ContactsContract;
import android.telecom.TelecomManager;
import android.telephony.SmsManager;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * Calls, text messages and contacts on request from YOUR PC. Needs, on the phone: "Let my PC control this phone",
 * "Allow calls and messages", and Android's own permissions (asked for from the app). The PC asks you before every
 * call or SMS it sends (with the number and the text). Codes that look like one-time passwords are hidden before
 * messages leave the phone.
 */
public final class Comms {
    private Comms() {}

    public static final Set<String> ACTIONS = new HashSet<String>(Arrays.asList(
            "call", "end_call", "send_sms", "read_messages", "find_contact"));
    private static final int MAX_MESSAGES = 10;

    static Object[] run(Context c, String action, Map<String, Object> args) {
        switch (action) {
            case "call": return call(c, Json.str(args, "number", ""));
            case "end_call": return endCall(c);
            case "send_sms": return sms(c, Json.str(args, "number", ""), Json.str(args, "text", ""));
            case "read_messages": return read(c, number(args, "count", 5), Json.str(args, "from", ""));
            case "find_contact": return find(c, Json.str(args, "name", ""));
            default: return new Object[]{false, "unknown"};
        }
    }

    private static int number(Map<String, Object> args, String key, int fallback) {
        Object v = args == null ? null : args.get(key);
        return v instanceof Number ? ((Number) v).intValue() : fallback;
    }

    private static boolean granted(Context c, String permission) {
        return Build.VERSION.SDK_INT < 23 || c.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    static boolean looksLikeNumber(String s) {
        return s.replaceAll("[\\s()+\\-.]", "").matches("\\d{3,15}");
    }

    /** "Ammu" -> her number (the best match in your contacts); a number stays as it is. */
    static String[] resolve(Context c, String who) {
        if (who.contains("*") || who.contains("#")) return null;   // never USSD / service codes (money, forwarding)
        if (looksLikeNumber(who)) return new String[]{who.trim(), who.trim()};
        if (!granted(c, "android.permission.READ_CONTACTS")) return null;
        List<String[]> found = contacts(c, who, 1);
        return found.isEmpty() ? null : found.get(0);
    }

    private static List<String[]> contacts(Context c, String name, int limit) {
        List<String[]> out = new ArrayList<String[]>();
        ContentResolver resolver = c.getContentResolver();
        Cursor cur = resolver.query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                new String[]{ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER},
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " LIKE ?", new String[]{"%" + name.trim() + "%"},
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " ASC");
        if (cur == null) return out;
        try {
            while (cur.moveToNext() && out.size() < limit) out.add(new String[]{cur.getString(0), cur.getString(1)});
        } finally {
            cur.close();
        }
        return out;
    }

    private static Object[] call(Context c, String who) {
        if (who.trim().isEmpty()) return new Object[]{false, "call whom?"};
        String[] target = resolve(c, who);
        if (target == null) return new Object[]{false, "no contact called '" + who + "' (or contacts permission is off)"};
        Uri tel = Uri.parse("tel:" + Uri.encode(target[1]));
        if (granted(c, "android.permission.CALL_PHONE")) {
            return PhoneActions.start(c, new Intent(Intent.ACTION_CALL, tel), "Calling " + target[0] + ".");
        }
        return PhoneActions.start(c, new Intent(Intent.ACTION_DIAL, tel),
                "Opened the dialer with " + target[0] + " - tap call (allow 'Phone' for JARVIS Mobile to call directly).");
    }

    private static Object[] endCall(Context c) {
        if (Build.VERSION.SDK_INT < 28) return new Object[]{false, "ending calls needs Android 9 or newer"};
        if (!granted(c, "android.permission.ANSWER_PHONE_CALLS")) return new Object[]{false, "allow 'Phone' for JARVIS Mobile first"};
        TelecomManager telecom = (TelecomManager) c.getSystemService(Context.TELECOM_SERVICE);
        boolean ended = telecom != null && telecom.endCall();
        return new Object[]{ended, ended ? "Call ended." : "there was no call to end"};
    }

    private static Object[] sms(Context c, String who, String text) {
        if (text.trim().isEmpty()) return new Object[]{false, "no message text"};
        String[] target = resolve(c, who);
        if (target == null) return new Object[]{false, "no contact called '" + who + "' (or contacts permission is off)"};
        if (!granted(c, "android.permission.SEND_SMS")) {
            Intent compose = new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(target[1])));
            compose.putExtra("sms_body", text);
            return PhoneActions.start(c, compose, "Opened the message to " + target[0] + " - tap send.");
        }
        SmsManager manager = Build.VERSION.SDK_INT >= 31 ? c.getSystemService(SmsManager.class) : SmsManager.getDefault();
        ArrayList<String> parts = manager.divideMessage(text);
        manager.sendMultipartTextMessage(target[1], null, parts, null, null);
        return new Object[]{true, "Sent to " + target[0] + "."};
    }

    /** One-time codes never leave the phone. */
    static String hideCodes(String text) {
        return text == null ? "" : text.replaceAll("(?<!\\d)\\d{4,8}(?!\\d)", "••••");
    }

    private static Object[] read(Context c, int count, String from) {
        if (!granted(c, "android.permission.READ_SMS")) return new Object[]{false, "allow 'SMS' for JARVIS Mobile first"};
        count = Math.max(1, Math.min(count, MAX_MESSAGES));
        String where = null;
        String[] whereArgs = null;
        if (!from.trim().isEmpty()) {
            String[] target = resolve(c, from);
            String needle = target != null ? target[1].replaceAll("[^\\d]", "") : from.trim();
            if (needle.length() > 7) needle = needle.substring(needle.length() - 7);   // matches +88 / 0 prefixes
            where = "address LIKE ?";
            whereArgs = new String[]{"%" + needle + "%"};
        }
        Cursor cur = c.getContentResolver().query(Uri.parse("content://sms/inbox"),
                new String[]{"address", "body", "date"}, where, whereArgs, "date DESC");
        if (cur == null) return new Object[]{false, "the phone didn't return messages"};
        StringBuilder out = new StringBuilder();
        SimpleDateFormat when = new SimpleDateFormat("d MMM HH:mm", Locale.US);
        int n = 0;
        try {
            while (cur.moveToNext() && n < count) {
                String body = hideCodes(cur.getString(1));
                if (body.length() > 160) body = body.substring(0, 160) + "...";
                out.append(n == 0 ? "" : " | ").append(name(c, cur.getString(0))).append(" (")
                        .append(when.format(new Date(cur.getLong(2)))).append("): ").append(body);
                n++;
            }
        } finally {
            cur.close();
        }
        return new Object[]{true, n == 0 ? "No messages." : out.toString()};
    }

    private static String name(Context c, String address) {
        if (address == null) return "?";
        if (!granted(c, "android.permission.READ_CONTACTS")) return address;
        Cursor cur = c.getContentResolver().query(
                Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(address)),
                new String[]{ContactsContract.PhoneLookup.DISPLAY_NAME}, null, null, null);
        if (cur == null) return address;
        try {
            return cur.moveToFirst() ? cur.getString(0) : address;
        } finally {
            cur.close();
        }
    }

    private static Object[] find(Context c, String name) {
        if (name.trim().isEmpty()) return new Object[]{false, "whose number?"};
        if (!granted(c, "android.permission.READ_CONTACTS")) return new Object[]{false, "allow 'Contacts' for JARVIS Mobile first"};
        List<String[]> found = contacts(c, name, 5);
        if (found.isEmpty()) return new Object[]{false, "no contact called '" + name + "'"};
        StringBuilder out = new StringBuilder();
        for (String[] f : found) out.append(out.length() == 0 ? "" : "; ").append(f[0]).append(": ").append(f[1]);
        return new Object[]{true, out.toString()};
    }
}
