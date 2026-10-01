#!/usr/bin/env bash
# Copy Dean to the tablet over SSH and restart it.
#   ./deploy.sh <tablet-ip>              deploy and restart
#   ./deploy.sh <tablet-ip> --ask "..."  deploy, then test a question in text mode
set -euo pipefail
HOST=${1:?usage: ./deploy.sh <tablet-ip> [--ask "question" ...]}
shift
SSH=(ssh -p 8022 "$HOST")
cd "$(dirname "$0")"

"${SSH[@]}" 'mkdir -p ~/assistant ~/.termux/boot'
scp -q -P 8022 *.py run.sh termux/bridge.py "$HOST:assistant/"
scp -q -P 8022 termux/boot-01-services "$HOST:.termux/boot/01-services"
"${SSH[@]}" 'chmod 700 ~/assistant/run.sh ~/.termux/boot/01-services
  grep -q dean-autostart ~/.bashrc 2>/dev/null || echo "note: add termux/bashrc-snippet.sh to ~/.bashrc"'

if [ $# -gt 0 ]; then
  # quote each argument for the remote shell
  printf -v ARGS '%q ' "$@"
  "${SSH[@]}" "proot-distro login debian -- env TZ=\$(getprop persist.sys.timezone) \
    /opt/dean/bin/python /data/data/com.termux/files/home/assistant/dean.py $ARGS"
fi

# restart the live assistant in place; the on-screen tmux view stays attached
"${SSH[@]}" 'tmux respawn-pane -k -t dean ~/assistant/run.sh 2>/dev/null || pkill -f "assistant/[d]ean.py" || true'
echo "deployed to $HOST; Dean is restarting"
