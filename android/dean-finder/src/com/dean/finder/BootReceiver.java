package com.dean.finder;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Reconnects after the phone restarts or the app is updated. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        ListenService.start(ctx);
    }
}
