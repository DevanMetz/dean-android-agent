package com.dean.sensors;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.util.SparseArray;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Scans for Govee thermo-hygrometer broadcasts (company id 0xEC88) and serves the
 * latest reading per sensor at http://127.0.0.1:8765/ (tablet only) as
 * {"status": ..., "adverts_heard": n, "sensors": [{name, temp_c, temp_f, humidity, battery, rssi, time}]}.
 */
public class SensorService extends Service {
    static final String TAG = "DeanSensors";
    static final int GOVEE = 0xEC88;
    static final int PORT = 8765;
    static final Map<String, JSONObject> latest = Collections.synchronizedMap(new LinkedHashMap<>());
    static volatile String status = "starting";
    static final boolean DEBUG_ALL = false; // log every advertisement (diagnostics)
    static volatile long adverts;

    private final Handler main = new Handler(Looper.getMainLooper());
    private BluetoothLeScanner scanner;
    private ServerSocket server;

    static void start(Context ctx) {
        Intent i = new Intent(ctx, SensorService.class);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
        else ctx.startService(i);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("scan", "Reading sensors",
                NotificationManager.IMPORTANCE_MIN));
        Notification n = new Notification.Builder(this, "scan")
                .setSmallIcon(android.R.drawable.ic_menu_view)
                .setContentTitle("Dean Sensors")
                .setContentText("Reading Bluetooth thermometers")
                .setContentIntent(PendingIntent.getActivity(this, 0,
                        new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE))
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(1, n);
        startServer();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startScan();
        return START_STICKY;
    }

    // ----- Bluetooth -----

    private final ScanCallback callback = new ScanCallback() {
        @Override
        public void onScanResult(int type, ScanResult r) {
            handle(r);
        }

        @Override
        public void onScanFailed(int errorCode) {
            status = "scan failed (" + errorCode + "), retrying";
            Log.w(TAG, status);
            scanner = null;
            main.postDelayed(SensorService.this::startScan, 30000);
        }
    };

    private void startScan() {
        if (scanner != null) return;
        try {
            BluetoothManager bm = getSystemService(BluetoothManager.class);
            if (bm == null || bm.getAdapter() == null || !bm.getAdapter().isEnabled()) {
                status = "Bluetooth is off";
                main.postDelayed(this::startScan, 30000);
                return;
            }
            scanner = bm.getAdapter().getBluetoothLeScanner();
            // a filter keeps scanning running while the screen is off
            ScanSettings settings = new ScanSettings.Builder()
                    .setScanMode(ScanSettings.SCAN_MODE_BALANCED).build();
            // Unfiltered: Dean's tablet keeps its screen on, so Android doesn't pause the
            // scan, and it avoids relying on manufacturer-id filtering by the BT stack.
            scanner.startScan(null, settings, callback);
            status = "scanning";
        } catch (SecurityException e) {
            status = "Bluetooth permission missing";
            scanner = null;
        }
    }

    private void handle(ScanResult r) {
        if (r.getScanRecord() == null) return;
        SparseArray<byte[]> mfg = r.getScanRecord().getManufacturerSpecificData();
        adverts++;
        if (DEBUG_ALL) {
            StringBuilder ids = new StringBuilder();
            for (int i = 0; i < mfg.size(); i++) {
                ids.append(String.format("%04X:", mfg.keyAt(i)));
                for (byte b : mfg.valueAt(i)) ids.append(String.format("%02X", b));
                ids.append(' ');
            }
            Log.i(TAG, "seen " + r.getDevice().getAddress() + " name=" + r.getScanRecord().getDeviceName()
                    + " rssi=" + r.getRssi() + " mfg=" + ids);
        }
        byte[] d = mfg.get(GOVEE);
        if (d == null || d.length < 5) return;
        String name = r.getScanRecord().getDeviceName();
        if (name == null) name = r.getDevice().getAddress();
        // H5072/H5075/H5101/... : 3-byte packed value, then battery %.
        // value = temp(0.1 °C) * 1000 + humidity(0.1 %), top bit set for below zero
        int value = ((d[1] & 0xFF) << 16) | ((d[2] & 0xFF) << 8) | (d[3] & 0xFF);
        boolean negative = (value & 0x800000) != 0;
        value &= 0x7FFFFF;
        double tempC = (value / 1000) / 10.0 * (negative ? -1 : 1);
        double humidity = (value % 1000) / 10.0;
        int battery = d[4] & 0x7F;
        if (humidity > 100 || tempC < -40 || tempC > 85) return; // not this format
        try {
            JSONObject o = new JSONObject();
            o.put("name", name);
            o.put("address", r.getDevice().getAddress());
            o.put("temp_c", tempC);
            o.put("temp_f", Math.round((tempC * 9 / 5 + 32) * 10) / 10.0);
            o.put("humidity", humidity);
            o.put("battery", battery);
            o.put("rssi", r.getRssi());
            o.put("time", System.currentTimeMillis() / 1000);
            latest.put(r.getDevice().getAddress(), o);
            status = "scanning, " + latest.size() + " sensor(s)";
        } catch (Exception ignored) {
        }
    }

    // ----- local HTTP: GET / -> JSON array of the latest readings -----

    private void startServer() {
        new Thread(() -> {
            try {
                server = new ServerSocket(PORT, 8, InetAddress.getByName("127.0.0.1"));
                while (!server.isClosed()) {
                    try (Socket s = server.accept()) {
                        s.setSoTimeout(2000);
                        s.getInputStream().read(new byte[1024]); // request line; any path works
                        JSONArray arr = new JSONArray();
                        synchronized (latest) {
                            for (JSONObject o : latest.values()) arr.put(o);
                        }
                        JSONObject all = new JSONObject();
                        all.put("status", status);
                        all.put("adverts_heard", adverts);
                        all.put("sensors", arr);
                        byte[] body = all.toString().getBytes("UTF-8");
                        OutputStream out = s.getOutputStream();
                        out.write(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                + "Content-Length: " + body.length + "\r\nConnection: close\r\n\r\n")
                                .getBytes("UTF-8"));
                        out.write(body);
                    } catch (Exception e) {
                        Log.w(TAG, "request failed: " + e);
                    }
                }
            } catch (Exception e) {
                status = "server failed: " + e.getMessage();
            }
        }, "http").start();
    }

    @Override
    public void onDestroy() {
        try {
            if (scanner != null) scanner.stopScan(callback);
        } catch (SecurityException ignored) {
        }
        try {
            if (server != null) server.close();
        } catch (Exception ignored) {
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
