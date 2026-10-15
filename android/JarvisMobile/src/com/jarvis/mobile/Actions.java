package com.jarvis.mobile;

import android.app.Activity;
import android.os.Handler;
import android.os.Looper;

import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Runs signed commands off the UI thread and reports back on it. Commands that change the PC are signed with the
 * biometric-gated key; if the Keystore says the user must authenticate again, we surface that to the Activity,
 * which shows the device's own fingerprint / PIN prompt and then retries. The PC still does all its own checks.
 */
public final class Actions {
    public interface Done {
        void result(Api.Result result, Exception error);
    }

    public interface NeedsAuth {
        void authenticate(Runnable retry, Done giveUp);
    }

    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Store store;
    private NeedsAuth needsAuth;

    public Actions(Store store) {
        this.store = store;
    }

    public void setAuthHandler(NeedsAuth handler) {
        this.needsAuth = handler;
    }

    private static boolean isAuthError(Throwable e) {
        while (e != null) {
            String n = e.getClass().getName();
            if (n.contains("UserNotAuthenticated") || n.contains("KeyPermanentlyInvalidated")) return true;
            e = e.getCause();
        }
        return false;
    }

    public void run(final Pc pc, final boolean away, final String command, final Map<String, Object> args,
                    final Done done) {
        execute(pc, away, command, args, done, true);
    }

    private void execute(final Pc pc, final boolean away, final String command, final Map<String, Object> args,
                         final Done done, final boolean allowAuthRetry) {
        pool.execute(new Runnable() {
            @Override public void run() {
                try {
                    Api.Result result = Api.command(pc, away, pc.id, command, args);
                    main.post(() -> done.result(result, null));
                } catch (final Exception e) {
                    if (allowAuthRetry && isAuthError(e) && needsAuth != null) {
                        main.post(() -> needsAuth.authenticate(
                                () -> execute(pc, away, command, args, done, false),
                                (r, err) -> done.result(r, e)));
                    } else {
                        main.post(() -> done.result(null, e));
                    }
                }
            }
        });
    }

    public void runOnPool(Runnable job) {
        pool.execute(job);
    }

    public void onMain(Runnable job) {
        main.post(job);
    }
}
