package com.dean.sensors;

import android.app.Activity;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

/** Shows the scanner status and the latest reading from each sensor. */
public class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView body;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        if (Build.VERSION.SDK_INT >= 31) {
            requestPermissions(new String[]{"android.permission.BLUETOOTH_SCAN",
                    "android.permission.POST_NOTIFICATIONS"}, 1);
        } else {
            requestPermissions(new String[]{"android.permission.ACCESS_FINE_LOCATION"}, 1);
        }
        SensorService.start(this);

        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad * 2, pad, pad);
        TextView title = new TextView(this);
        title.setText("Dean Sensors");
        title.setTextSize(28);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(title);
        body = new TextView(this);
        body.setTextSize(18);
        body.setPadding(0, pad, 0, 0);
        root.addView(body);
        setContentView(root);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        SensorService.start(this);
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(new Runnable() {
            @Override
            public void run() {
                StringBuilder sb = new StringBuilder("Status: " + SensorService.status + "\n\n");
                synchronized (SensorService.latest) {
                    for (JSONObject o : SensorService.latest.values()) {
                        long age = System.currentTimeMillis() / 1000 - o.optLong("time");
                        sb.append(o.optString("name")).append("\n  ")
                                .append(o.optDouble("temp_f")).append(" °F  ·  ")
                                .append(o.optDouble("humidity")).append(" %  ·  battery ")
                                .append(o.optInt("battery")).append(" %  ·  ")
                                .append(age).append(" s ago\n\n");
                    }
                }
                body.setText(sb.toString());
                handler.postDelayed(this, 2000);
            }
        });
    }

    @Override
    protected void onPause() {
        handler.removeCallbacksAndMessages(null);
        super.onPause();
    }
}
