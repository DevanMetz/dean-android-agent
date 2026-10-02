"""Runs natively in Termux (not in the Debian proot) and executes Termux:API
commands for Dean. Starting those commands under proot costs ~2.5 s each;
natively it's ~0.35 s.

Listens on a Unix socket that only the Termux app's user can open.
Protocol: one JSON request per connection
  {"args": ["termux-battery-status"], "stdin": null, "timeout": 30}
and one JSON reply {"rc": 0, "stdout": "..."}.
"""

import json
import os
import socketserver
import subprocess

SOCKET = os.path.expanduser("~/assistant/bridge.sock")
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
# the one Activity Manager command Dean may run: restart the Dean Sensors app's scanner
RESTART_SENSORS = ["am", "broadcast", "-n", "com.dean.sensors/.BootReceiver",
                   "-a", "com.dean.sensors.START"]
# ...and bring Termux (Dean's screen) back to the front
SHOW_TERMUX = ["am", "start", "-n", "com.termux/.app.TermuxActivity"]
ALLOWED = {
    "termux-battery-status", "termux-brightness", "termux-camera-photo", "termux-location",
    "termux-sensor", "termux-torch", "termux-tts-speak", "termux-volume",
    "termux-wifi-connectioninfo",
}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline())
            args = [str(a) for a in req["args"]]
            if args[0] not in ALLOWED and args not in (RESTART_SENSORS, SHOW_TERMUX):
                raise ValueError(f"command not allowed: {args[0]}")
            r = subprocess.run([f"{PREFIX}/bin/{args[0]}", *args[1:]], capture_output=True,
                               text=True, input=req.get("stdin"),
                               timeout=float(req.get("timeout", 30)))
            reply = {"rc": r.returncode, "stdout": r.stdout}
        except subprocess.TimeoutExpired:
            reply = {"rc": -1, "stdout": "", "error": "timed out"}
        except Exception as e:
            reply = {"rc": -1, "stdout": "", "error": f"{type(e).__name__}: {e}"}
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the caller gave up waiting


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if __name__ == "__main__":
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    os.umask(0o077)  # socket readable/writable by the Termux user only
    with Server(SOCKET, Handler) as server:
        server.serve_forever()
