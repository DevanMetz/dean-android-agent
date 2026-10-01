package com.dean.finder;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Foreground service that streams messages from the private ntfy channel.
 * "ring" makes the phone ring (see Ringer); "stop" stops it.
 */
public class ListenService extends Service {
    static final String CHANNEL_LISTEN = "listen";
    static volatile String status = "starting";

    private volatile boolean running;
    private Thread worker;

    static void start(Context ctx) {
        Intent i = new Intent(ctx, ListenService.class);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
        else ctx.startService(i);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_LISTEN, "Waiting for Dean", NotificationManager.IMPORTANCE_MIN);
        ch.setShowBadge(false);
        nm.createNotificationChannel(ch);

        PendingIntent open = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CHANNEL_LISTEN)
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setContentTitle("Dean Finder is ready")
                .setContentText("Say \"hey Dean, find my Pixel\" to make this phone ring")
                .setContentIntent(open)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(1, n);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::listenLoop, "ntfy-listener");
            worker.start();
        }
        return START_STICKY;
    }

    private void listenLoop() {
        long backoff = 2000;
        String since = null; // after the first connection, resume from the last message seen
        while (running) {
            HttpURLConnection c = null;
            try {
                String url = "https://ntfy.sh/" + Config.TOPIC + "/json"
                        + (since != null ? "?since=" + since : "");
                c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(90000); // ntfy sends a keepalive every ~45 s
                BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"));
                status = "connected";
                backoff = 2000;
                String line;
                while (running && (line = r.readLine()) != null) {
                    JSONObject m = new JSONObject(line);
                    if (m.has("id")) since = m.getString("id");
                    if (!"message".equals(m.optString("event"))) continue;
                    // ignore requests older than 2 minutes (e.g. delivered after a long outage)
                    long age = System.currentTimeMillis() / 1000 - m.optLong("time");
                    if (age > 120) continue;
                    String msg = m.optString("message").trim().toLowerCase();
                    Log.i("DeanFinder", "message: " + msg);
                    if (msg.startsWith("ring")) Ringer.start(this, msg.contains("test") ? 5 : 60);
                    else if (msg.startsWith("stop")) Ringer.stop(this);
                }
            } catch (Exception e) {
                status = "reconnecting (" + e.getClass().getSimpleName() + ")";
            } finally {
                if (c != null) c.disconnect();
            }
            if (!running) break;
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException ignored) {
                break;
            }
            backoff = Math.min(backoff * 2, 60000);
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
