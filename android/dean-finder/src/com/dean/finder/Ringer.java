package com.dean.finder;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.hardware.camera2.CameraManager;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.media.ToneGenerator;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.util.Log;
import android.os.Vibrator;

/**
 * Rings on the alarm stream at full volume (alarms play even when the phone is on
 * silent or vibrate), vibrates and blinks the flashlight until stopped.
 */
public final class Ringer {
    static final String CHANNEL_RING = "ring";
    private static final int NOTIFICATION_ID = 2;
    private static final Handler main = new Handler(Looper.getMainLooper());

    private static MediaPlayer player;
    private static ToneGenerator tones;
    private static int savedVolume = -1;
    private static boolean torchOn;
    private static final Runnable autoStop = () -> stop(null);
    private static Runnable blink;
    private static Context appContext; // for the auto-stop timer

    private Ringer() {}

    static synchronized void start(Context ctx, int seconds) {
        final Context app = ctx.getApplicationContext();
        main.post(() -> startOnMain(app, seconds));
    }

    private static synchronized void startOnMain(Context ctx, int seconds) {
        appContext = ctx;
        stopOnMain(ctx);
        AudioManager am = ctx.getSystemService(AudioManager.class);
        savedVolume = am.getStreamVolume(AudioManager.STREAM_ALARM);
        am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0);

        // the phone's alarm sound, else its ringtone or notification sound
        int[] types = {RingtoneManager.TYPE_ALARM, RingtoneManager.TYPE_RINGTONE,
                RingtoneManager.TYPE_NOTIFICATION};
        for (int type : types) {
            Uri tone = RingtoneManager.getDefaultUri(type);
            if (tone == null) continue;
            try {
                player = new MediaPlayer();
                player.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build());
                player.setDataSource(ctx, tone);
                player.setLooping(true);
                player.prepare();
                player.start();
                break;
            } catch (Exception e) {
                Log.w("DeanFinder", "can't play " + tone + ": " + e);
                player.release();
                player = null;
            }
        }
        if (player == null) {  // no usable sound file: built-in ring cadence
            tones = new ToneGenerator(AudioManager.STREAM_ALARM, ToneGenerator.MAX_VOLUME);
            tones.startTone(ToneGenerator.TONE_SUP_RINGTONE);
        }

        Vibrator v = ctx.getSystemService(Vibrator.class);
        if (v != null) v.vibrate(VibrationEffect.createWaveform(new long[]{0, 700, 500}, 0));

        startBlinking(ctx);
        showNotification(ctx);
        main.postDelayed(autoStop, seconds * 1000L);
        ListenService.status = "ringing";
        Log.i("DeanFinder", "ringing for " + seconds + " s, alarm volume was " + savedVolume + ", sound=" + (player != null ? "file" : "built-in tone"));
    }

    static void stop(Context ctx) {
        main.post(() -> stopOnMain(ctx));
    }

    private static synchronized void stopOnMain(Context ctx) {
        main.removeCallbacks(autoStop);
        if (blink != null) main.removeCallbacks(blink);
        blink = null;
        if (player != null) {
            try {
                player.stop();
            } catch (Exception ignored) {
            }
            player.release();
            player = null;
        }
        if (tones != null) {
            tones.stopTone();
            tones.release();
            tones = null;
        }
        Context c = ctx != null ? ctx : appContext;
        if (c == null) return;
        if (savedVolume >= 0) {
            c.getSystemService(AudioManager.class).setStreamVolume(AudioManager.STREAM_ALARM, savedVolume, 0);
            savedVolume = -1;
        }
        Vibrator v = c.getSystemService(Vibrator.class);
        if (v != null) v.cancel();
        setTorch(c, false);
        c.getSystemService(NotificationManager.class).cancel(NOTIFICATION_ID);
        if ("ringing".equals(ListenService.status)) ListenService.status = "connected";
        Log.i("DeanFinder", "stopped; alarm volume now " + c.getSystemService(AudioManager.class).getStreamVolume(AudioManager.STREAM_ALARM));
    }

    private static void startBlinking(Context ctx) {
        blink = new Runnable() {
            @Override
            public void run() {
                setTorch(ctx, !torchOn);
                if (blink == this) main.postDelayed(this, 400);
            }
        };
        main.post(blink);
    }

    private static void setTorch(Context ctx, boolean on) {
        try {
            CameraManager cm = ctx.getSystemService(CameraManager.class);
            for (String id : cm.getCameraIdList()) {
                Boolean flash = cm.getCameraCharacteristics(id).get(
                        android.hardware.camera2.CameraCharacteristics.FLASH_INFO_AVAILABLE);
                if (Boolean.TRUE.equals(flash)) {
                    cm.setTorchMode(id, on);
                    torchOn = on;
                    return;
                }
            }
        } catch (Exception ignored) {
            // camera busy or no flash; ringing still works
        }
    }

    private static void showNotification(Context ctx) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_RING, "Ringing", NotificationManager.IMPORTANCE_HIGH);
        ch.setSound(null, null); // the MediaPlayer makes the noise
        nm.createNotificationChannel(ch);
        PendingIntent stop = PendingIntent.getBroadcast(ctx, 0,
                new Intent(ctx, StopReceiver.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent open = PendingIntent.getActivity(ctx, 1,
                new Intent(ctx, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(ctx, CHANNEL_RING)
                .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle("Found me!")
                .setContentText("Dean is ringing this phone. Tap Stop.")
                .setCategory(Notification.CATEGORY_ALARM)
                .setContentIntent(open)
                .setDeleteIntent(stop)
                .addAction(new Notification.Action.Builder(null, "Stop", stop).build())
                .build();
        nm.notify(NOTIFICATION_ID, n);
    }
}
