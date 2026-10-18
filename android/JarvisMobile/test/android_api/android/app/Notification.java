package android.app;
import android.content.Context;
import android.os.Bundle;
public class Notification {
    public static final String EXTRA_TITLE = "android.title", EXTRA_TEXT = "android.text";
    public Bundle extras;
    public static class Action {
        public static class Builder {
            public Builder(android.graphics.drawable.Icon icon, CharSequence title, PendingIntent intent) {}
            public Action build() { return new Action(); }
        }
    }
    public static class Builder {
        public Builder(Context c) {}
        public Builder(Context c, String channel) {}
        public Builder setSmallIcon(int i) { return this; }
        public Builder setContentTitle(CharSequence t) { return this; }
        public Builder setContentText(CharSequence t) { return this; }
        public Builder setContentIntent(PendingIntent p) { return this; }
        public Builder setAutoCancel(boolean b) { return this; }
        public Builder setOngoing(boolean b) { return this; }
        public Builder addAction(Action a) { return this; }
        public Notification build() { return new Notification(); }
    }
}
