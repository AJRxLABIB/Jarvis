package com.jarvis.mobile;

import android.app.admin.DeviceAdminReceiver;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;

/**
 * "Lock my phone from the PC". Android only lets an app lock the screen after YOU make it a device administrator
 * (Settings > Security > Device admin apps > JARVIS Mobile). The only power requested is "lock the screen" - this app
 * cannot wipe the phone, change the PIN or read anything through it. Switch it off in the same place any time.
 */
public final class LockAdmin extends DeviceAdminReceiver {
    static ComponentName component(Context c) {
        return new ComponentName(c, LockAdmin.class);
    }

    static boolean active(Context c) {
        DevicePolicyManager dpm = (DevicePolicyManager) c.getSystemService(Context.DEVICE_POLICY_SERVICE);
        return dpm != null && dpm.isAdminActive(component(c));
    }

    /** Locks the screen now; when the admin isn't on yet, opens Android's own screen to switch it on. */
    static Object[] lockNow(Context c) {
        DevicePolicyManager dpm = (DevicePolicyManager) c.getSystemService(Context.DEVICE_POLICY_SERVICE);
        if (dpm == null) return new Object[]{false, "this phone can't lock from an app"};
        if (!dpm.isAdminActive(component(c))) {
            Intent ask = new Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
                    .putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, component(c))
                    .putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION,
                            "Lets your PC lock this phone's screen. JARVIS Mobile gets no other power from this.");
            Object[] shown = PhoneActions.start(c, ask, "Asked on the phone to allow 'lock the screen' - tap Activate there, then try again.");
            return new Object[]{false, "lock isn't allowed on the phone yet. " + shown[1]};
        }
        dpm.lockNow();
        return new Object[]{true, "The phone is locked."};
    }
}
