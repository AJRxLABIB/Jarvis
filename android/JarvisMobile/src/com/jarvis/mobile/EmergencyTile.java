package com.jarvis.mobile;

import android.os.Handler;
import android.os.Looper;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

import java.util.LinkedHashMap;

/**
 * Quick Settings tile: EMERGENCY STOP for the paired PC. One tap sends the same signed "emergency_stop" command the app's red
 * button sends (it only stops things; it is allowed for any trusted phone and needs no fingerprint). The tile shows what
 * happened - it never claims the PC stopped unless the PC answered ok.
 */
public class EmergencyTile extends TileService {
    private final Handler main = new Handler(Looper.getMainLooper());

    @Override public void onStartListening() {
        show("JARVIS stop", Tile.STATE_INACTIVE);
    }

    @Override public void onClick() {
        final Pc pc = new Store(this).current();
        if (pc == null) { show("No PC paired", Tile.STATE_UNAVAILABLE); return; }
        show("Stopping...", Tile.STATE_ACTIVE);
        new Thread(new Runnable() {
            @Override public void run() {
                String label;
                try {
                    Api.Result r = Api.command(pc, false, pc.id, "emergency_stop", new LinkedHashMap<String, Object>());
                    label = r != null && r.ok() ? "Stopped" : "PC refused";
                } catch (Exception e) {
                    label = "PC not reached";
                }
                final String shown = label;
                main.post(new Runnable() { @Override public void run() { show(shown, Tile.STATE_INACTIVE); } });
            }
        }).start();
    }

    private void show(String label, int state) {
        try {
            Tile tile = getQsTile();
            if (tile == null) return;
            tile.setLabel(label);
            tile.setState(state);
            tile.updateTile();
        } catch (Exception ignored) {
            // the tile service may already be unbound
        }
    }
}
