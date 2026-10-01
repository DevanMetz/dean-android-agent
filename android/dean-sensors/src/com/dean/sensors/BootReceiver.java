package com.dean.sensors;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Restarts scanning after a reboot or an app update. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        SensorService.start(ctx);
    }
}
