package android.widget;
public abstract class CompoundButton extends Button {
    public CompoundButton(android.content.Context c) { super(c); }
    public interface OnCheckedChangeListener { void onCheckedChanged(CompoundButton b, boolean checked); }
    public void setChecked(boolean on) {}
    public boolean isChecked() { return false; }
    public void setOnCheckedChangeListener(OnCheckedChangeListener l) {}
}
