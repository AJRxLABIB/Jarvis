package android.content;
public interface DialogInterface {
    interface OnClickListener { void onClick(DialogInterface d, int which); }
    interface OnDismissListener { void onDismiss(DialogInterface d); }
    void dismiss();
    void cancel();
}
