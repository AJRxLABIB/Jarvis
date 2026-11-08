package com.jarvis.mobile;

import android.os.Handler;
import android.os.Looper;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

/**
 * Quick Settings tile: sends the Wake-on-LAN packet to the paired PC (same as the Wake button in the app). It wakes a PC that is
 * ASLEEP or hibernating when its network card and BIOS allow it; it cannot power on a PC that is fully shut down unless the
 * firmware supports that. The tile says "Wake sent", never "PC is on" - only the app's status screen knows that.
 */
public class WakeTile extends TileService {
    private final Handler main = new Handler(Looper.getMainLooper());

    @Override public void onStartListening() {
        show("Wake PC", Tile.STATE_INACTIVE);
    }

    @Override public void onClick() {
        final Pc pc = new Store(this).current();
        if (pc == null || pc.mac == null || pc.mac.isEmpty()) { show("Set up Wake first", Tile.STATE_UNAVAILABLE); return; }
        show("Sending...", Tile.STATE_ACTIVE);
        new Thread(new Runnable() {
            @Override public void run() {
                String label;
                try {
                    Wol.send(Wol.parseMac(pc.mac), pc.wakeTargets(), pc.publicPort > 0 ? pc.publicPort : 9);
                    label = "Wake sent";
                } catch (Exception e) {
                    label = "Wake failed";
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
