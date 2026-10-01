package com.dean.finder;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** "Stop" button (or swiping away) on the ringing notification. */
public class StopReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        Ringer.stop(ctx);
    }
}
