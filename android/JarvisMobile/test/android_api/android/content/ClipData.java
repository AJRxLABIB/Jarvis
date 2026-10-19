package android.content;
public class ClipData { public static ClipData newPlainText(CharSequence l, CharSequence t) { return new ClipData(); }
    public int getItemCount() { return 0; } public Item getItemAt(int i) { return new Item(); }
    public static class Item { public CharSequence coerceToText(Context c) { return ""; } } }
