# Append to ~/.bashrc in Termux.
# dean-autostart: on the tablet screen (not SSH), show the Dean assistant
if [ -z "$TMUX" ] && [ -z "$SSH_CONNECTION" ] && [ -x ~/assistant/run.sh ]; then
  pgrep -x sshd >/dev/null || sshd; termux-wake-lock
  tmux has-session -t dean 2>/dev/null || tmux new-session -d -s dean ~/assistant/run.sh
  [ -z "$(tmux list-clients -t dean 2>/dev/null)" ] && exec tmux attach -t dean
fi
