package android.app.job;
import android.content.Context;
import android.content.SharedPreferences;
public abstract class JobService extends Context {
    public SharedPreferences getSharedPreferences(String name, int mode) { return null; }
    public Object getSystemService(String name) { return null; }
    public abstract boolean onStartJob(JobParameters p);
    public abstract boolean onStopJob(JobParameters p);
    public final void jobFinished(JobParameters p, boolean reschedule) {}
}
