package com.jarvis.mobile;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A tiny JSON reader/writer, so the app needs no libraries (no Gradle). Enough for the JARVIS Mobile
 * protocol: objects, arrays, strings, numbers, booleans, null. Not a general-purpose parser.
 */
public final class Json {
    private Json() {}

    // ------------------------------------------------------------------ writing

    public static String escape(String s) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.toString();
    }

    @SuppressWarnings("unchecked")
    public static String write(Object value) {
        StringBuilder b = new StringBuilder();
        writeValue(b, value);
        return b.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder b, Object value) {
        if (value == null) { b.append("null"); return; }
        if (value instanceof String) { b.append('"').append(escape((String) value)).append('"'); return; }
        if (value instanceof Boolean || value instanceof Integer || value instanceof Long) { b.append(value.toString()); return; }
        if (value instanceof Double || value instanceof Float) { b.append(value.toString()); return; }
        if (value instanceof Map) {
            b.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) value).entrySet()) {
                if (!first) b.append(',');
                first = false;
                b.append('"').append(escape(e.getKey())).append("\":");
                writeValue(b, e.getValue());
            }
            b.append('}');
            return;
        }
        if (value instanceof List) {
            b.append('[');
            boolean first = true;
            for (Object item : (List<Object>) value) {
                if (!first) b.append(',');
                first = false;
                writeValue(b, item);
            }
            b.append(']');
            return;
        }
        b.append('"').append(escape(value.toString())).append('"');
    }

    // ------------------------------------------------------------------ reading

    public static Object parse(String text) {
        P p = new P(text);
        p.ws();
        Object v = p.value();
        p.ws();
        return v;
    }

    /** parse() as a Map, or an empty map if it isn't an object. */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> object(String text) {
        try {
            Object v = parse(text);
            if (v instanceof Map) return (Map<String, Object>) v;
        } catch (RuntimeException ignored) {
        }
        return new LinkedHashMap<String, Object>();
    }

    public static String str(Map<String, Object> map, String key, String fallback) {
        Object v = map.get(key);
        return v == null ? fallback : v.toString();
    }

    public static boolean bool(Map<String, Object> map, String key) {
        Object v = map.get(key);
        return v instanceof Boolean ? (Boolean) v : "true".equals(String.valueOf(v));
    }

    private static final class P {
        private final String s;
        private int i;

        P(String s) { this.s = s; }

        void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }

        Object value() {
            ws();
            if (i >= s.length()) throw new RuntimeException("empty");
            char c = s.charAt(i);
            switch (c) {
                case '{': return obj();
                case '[': return arr();
                case '"': return string();
                case 't': i += 4; return Boolean.TRUE;
                case 'f': i += 5; return Boolean.FALSE;
                case 'n': i += 4; return null;
                default: return number();
            }
        }

        Map<String, Object> obj() {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            i++; // {
            ws();
            if (peek() == '}') { i++; return m; }
            while (true) {
                ws();
                String key = string();
                ws();
                i++; // :
                m.put(key, value());
                ws();
                char c = next();
                if (c == '}') break;
            }
            return m;
        }

        List<Object> arr() {
            List<Object> a = new ArrayList<Object>();
            i++; // [
            ws();
            if (peek() == ']') { i++; return a; }
            while (true) {
                a.add(value());
                ws();
                char c = next();
                if (c == ']') break;
            }
            return a;
        }

        String string() {
            StringBuilder b = new StringBuilder();
            i++; // "
            while (i < s.length()) {
                char c = s.charAt(i++);
                if (c == '"') break;
                if (c == '\\') {
                    char e = s.charAt(i++);
                    switch (e) {
                        case 'n': b.append('\n'); break;
                        case 'r': b.append('\r'); break;
                        case 't': b.append('\t'); break;
                        case 'b': b.append('\b'); break;
                        case 'f': b.append('\f'); break;
                        case 'u': b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                        default: b.append(e);
                    }
                } else {
                    b.append(c);
                }
            }
            return b.toString();
        }

        Object number() {
            int start = i;
            while (i < s.length() && "-+.eE0123456789".indexOf(s.charAt(i)) >= 0) i++;
            String num = s.substring(start, i);
            if (num.indexOf('.') >= 0 || num.indexOf('e') >= 0 || num.indexOf('E') >= 0) return Double.parseDouble(num);
            try { return Long.parseLong(num); } catch (NumberFormatException e) { return Double.parseDouble(num); }
        }

        char peek() { ws(); return i < s.length() ? s.charAt(i) : '\0'; }

        char next() { return i < s.length() ? s.charAt(i++) : '\0'; }
    }
}
