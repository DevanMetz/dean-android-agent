package com.dean.finder;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** One screen: status, a test button, a stop button and a battery-setting shortcut. */
public class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView status;
    private Button battery;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
        }
        ListenService.start(this);

        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad * 2, pad, pad);
        root.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView title = new TextView(this);
        title.setText("Dean Finder");
        title.setTextSize(28);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(title);

        TextView about = new TextView(this);
        about.setText("Say \"hey Dean, find my Pixel\" and this phone rings at full alarm volume, "
                + "even on silent, until you tap Stop.");
        about.setTextSize(16);
        about.setPadding(0, pad / 2, 0, pad);
        root.addView(about);

        status = new TextView(this);
        status.setTextSize(16);
        root.addView(status);

        Button test = new Button(this);
        test.setText("Test ring (5 seconds)");
        test.setOnClickListener(v -> Ringer.start(this, 5));
        root.addView(test);

        Button stop = new Button(this);
        stop.setText("Stop ringing");
        stop.setOnClickListener(v -> Ringer.stop(this));
        root.addView(stop);

        battery = new Button(this);
        battery.setText("Let it run in the background");
        battery.setOnClickListener(v -> startActivity(new Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:" + getPackageName()))));
        root.addView(battery);

        setContentView(root);
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(new Runnable() {
            @Override
            public void run() {
                boolean exempt = getSystemService(PowerManager.class)
                        .isIgnoringBatteryOptimizations(getPackageName());
                battery.setEnabled(!exempt);
                battery.setText(exempt ? "Background running allowed ✓" : "Let it run in the background");
                status.setText("Status: " + ListenService.status
                        + (exempt ? "" : "\nTap the button below so Android doesn't put it to sleep."));
                handler.postDelayed(this, 1000);
            }
        });
    }

    @Override
    protected void onPause() {
        handler.removeCallbacksAndMessages(null);
        super.onPause();
    }
}
