package com.jarvis.mobile;

import android.content.Context;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** JARVIS look, built in code (no XML): dark, cyan accents, rounded cards. Keeps the screens readable. */
public final class Ui {
    public static final int BG = Color.rgb(2, 5, 11), PANEL = Color.rgb(7, 18, 31), EDGE = Color.rgb(21, 58, 87),
            CYAN = Color.rgb(56, 189, 248), GOLD = Color.rgb(245, 182, 66), TEXT = Color.rgb(214, 230, 245),
            DIM = Color.rgb(111, 139, 166), GREEN = Color.rgb(52, 211, 153), RED = Color.rgb(248, 113, 113);

    private Ui() {}

    public static int dp(Context c, int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, c.getResources().getDisplayMetrics());
    }

    public static TextView text(Context c, String s, int size, int color) {
        TextView t = new TextView(c);
        t.setText(s);
        t.setTextSize(size);
        t.setTextColor(color);
        return t;
    }

    public static LinearLayout card(Context c) {
        LinearLayout box = new LinearLayout(c);
        box.setOrientation(LinearLayout.VERTICAL);
        int p = dp(c, 16);
        box.setPadding(p, p, p, p);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(PANEL);
        bg.setCornerRadius(dp(c, 14));
        bg.setStroke(dp(c, 1), EDGE);
        box.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, dp(c, 6), 0, dp(c, 6));
        box.setLayoutParams(lp);
        return box;
    }

    public static Button button(Context c, String label, int accent) {
        Button b = new Button(c);
        b.setAllCaps(false);
        b.setText(label);
        b.setTextColor(accent == CYAN || accent == GREEN ? BG : TEXT);
        b.setTextSize(16);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(accent);
        bg.setCornerRadius(dp(c, 12));
        b.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, dp(c, 5), 0, dp(c, 5));
        b.setLayoutParams(lp);
        b.setPadding(0, dp(c, 14), 0, dp(c, 14));
        return b;
    }

    public static Button chip(Context c, String label) {
        Button b = new Button(c);
        b.setAllCaps(false);
        b.setText(label);
        b.setTextColor(TEXT);
        b.setTextSize(13);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.rgb(11, 36, 56));
        bg.setCornerRadius(dp(c, 18));
        bg.setStroke(dp(c, 1), EDGE);
        b.setBackground(bg);
        b.setPadding(dp(c, 14), dp(c, 8), dp(c, 14), dp(c, 8));
        return b;
    }
}
