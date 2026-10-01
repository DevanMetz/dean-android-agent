#!/data/data/com.termux/files/usr/bin/bash
# Keeps Dean running inside tmux session "dean". Restarts audio and the
# assistant if either dies (e.g. Android muted the mic while Termux was hidden).
TZ_NAME=$(getprop persist.sys.timezone)
PULSE_SOCKET=$PREFIX/tmp/dean-pulse.socket
while true; do
  pkill -9 pulseaudio 2>/dev/null; sleep 1
  # audio goes over a socket in Termux's private storage, so no other app can tap the mic
  # (the Debian side needs enable-shm/enable-memfd = no in /etc/pulse/client.conf.d/)
  pulseaudio --start --exit-idle-time=-1 --load=module-sles-source \
    --load="module-native-protocol-unix socket=$PULSE_SOCKET auth-anonymous=1" 2>/dev/null
  # native helper that runs Termux:API commands quickly for Dean (see bridge.py)
  # (one supervisor loop, named "dean-bridge-loop", restarts it if it ever dies)
  pgrep -f "[d]ean-bridge-loop" >/dev/null ||
    bash -c 'while true; do python3 ~/assistant/bridge.py; sleep 2; done' dean-bridge-loop &
  proot-distro login debian -- env TZ="$TZ_NAME" PULSE_SERVER="unix:$PULSE_SOCKET" \
    /opt/dean/bin/python /data/data/com.termux/files/home/assistant/dean.py
  echo "Dean stopped (exit $?) - restarting in 5s..."
  sleep 5
done
