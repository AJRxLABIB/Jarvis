package android.service.quicksettings;
public class Tile { public static final int STATE_UNAVAILABLE = 0, STATE_INACTIVE = 1, STATE_ACTIVE = 2;
    public void setLabel(CharSequence l) {} public void setState(int s) {} public void updateTile() {} }
