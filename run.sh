#!/data/data/com.termux/files/usr/bin/bash
# Keeps Dean running inside tmux session "dean". Restarts audio and the
# assistant if either dies (e.g. Android muted the mic while Termux was hidden).
TZ_NAME=$(getprop persist.sys.timezone)
while true; do
  pkill -9 pulseaudio 2>/dev/null; sleep 1
  pulseaudio --start --exit-idle-time=-1 --load=module-sles-source \
    --load="module-native-protocol-tcp auth-ip-acl=127.0.0.1 auth-anonymous=1" 2>/dev/null
  proot-distro login debian -- env TZ="$TZ_NAME" PULSE_SERVER=tcp:127.0.0.1 \
    /opt/dean/bin/python /data/data/com.termux/files/home/assistant/dean.py
  echo "Dean stopped (exit $?) - restarting in 5s..."
  sleep 5
done
