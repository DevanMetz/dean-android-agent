"""Roku TV control over the local network (Roku External Control Protocol, port 8060).

The TV must allow it: Settings > System > Advanced system settings >
Control by mobile apps > Network access = "Default" (or "Permissive").

The TV's address lives in ~/assistant/roku.json ({"ip": "192.168.1.99"}); if the TV
moves, it's found again by probing the local /24 for port 8060.
"""

import json
import re
import socket
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROKU_FILE = Path("/data/data/com.termux/files/home/assistant/roku.json")
BLOCKED = ("The TV is blocking network control. On the TV: Settings > System > Advanced system "
           "settings > Control by mobile apps > Network access, set it to Default.")

# spoken names -> ECP key names
KEYS = {
    "home": "Home", "back": "Back", "select": "Select", "ok": "Select", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "play": "Play", "pause": "Play", "play_pause": "Play",
    "rewind": "Rev", "fast_forward": "Fwd", "replay": "InstantReplay", "info": "Info",
    "options": "Info", "volume_up": "VolumeUp", "volume_down": "VolumeDown", "mute": "VolumeMute",
    "channel_up": "ChannelUp", "channel_down": "ChannelDown", "power": "Power",
    "on": "PowerOn", "off": "PowerOff", "search": "Search",
}
INPUTS = {"hdmi1": "InputHDMI1", "hdmi2": "InputHDMI2", "hdmi3": "InputHDMI3",
          "hdmi4": "InputHDMI4", "antenna": "InputTuner", "tuner": "InputTuner", "av": "InputAV1"}


class RokuError(Exception):
    pass


class Roku:
    def __init__(self, ip=None):
        cfg = json.loads(ROKU_FILE.read_text()) if ROKU_FILE.exists() else {}
        self.ip = ip or cfg.get("ip")
        self.http = httpx.Client(timeout=4)

    # ----- transport -----

    def _url(self, path):
        if not self.ip:
            self.ip = self.find()
        return f"http://{self.ip}:8060/{path}"

    def _request(self, method, path):
        try:
            r = self.http.request(method, self._url(path))
        except httpx.HTTPError:
            # maybe the TV got a new address: look for it once, then retry
            old, self.ip = self.ip, self.find()
            if self.ip == old:
                raise RokuError("the TV isn't answering (is it unplugged or off Wi-Fi?)")
            r = self.http.request(method, self._url(path))
        if r.status_code == 403:
            raise RokuError(BLOCKED)
        r.raise_for_status()
        return r.text

    def get(self, path):
        return self._request("GET", path)

    def post(self, path):
        return self._request("POST", path)

    @staticmethod
    def find(subnet=None):
        """Probe x.y.z.1-254:8060 for a Roku and remember it."""
        if subnet is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the LAN address
                subnet = s.getsockname()[0].rsplit(".", 1)[0]
            finally:
                s.close()

        def probe(i):
            ip = f"{subnet}.{i}"
            try:
                r = httpx.get(f"http://{ip}:8060/", timeout=1.0)
                return ip if "roku" in r.text.lower() or r.status_code == 403 else None
            except httpx.HTTPError:
                return None

        with ThreadPoolExecutor(64) as pool:
            hits = [ip for ip in pool.map(probe, range(1, 255)) if ip]
        if not hits:
            raise RokuError("couldn't find a Roku on the network")
        ROKU_FILE.write_text(json.dumps({"ip": hits[0]}))
        return hits[0]

    # ----- queries -----

    @staticmethod
    def _tag(xml, tag):
        m = re.search(f"<{tag}>(.*?)</{tag}>", xml, re.S)
        return m.group(1).strip() if m else None

    def status(self):
        info = self.get("query/device-info")
        out = {"name": self._tag(info, "user-device-name"),
               "power": "on" if self._tag(info, "power-mode") == "PowerOn" else "off (standby)"}
        if out["power"] == "on":
            app = self.get("query/active-app")
            m = re.search(r"<app[^>]*>(.*?)</app>", app)
            out["app"] = m.group(1) if m else "home screen"
            try:
                media = self.get("query/media-player")
                state = re.search(r'state="(\w+)"', media)
                if state:
                    out["playback"] = state.group(1)
            except Exception:
                pass
        return out

    def apps(self):
        xml = self.get("query/apps")
        return {name: app_id for app_id, name in re.findall(r'<app id="(\d+)"[^>]*>(.*?)</app>', xml)}

    # ----- actions -----

    def press(self, key, times=1):
        for i in range(max(1, min(int(times), 50))):
            self.post(f"keypress/{key}")
            if times > 1:
                time.sleep(0.15)

    def launch(self, name):
        apps = self.apps()
        want = name.lower().replace("+", " plus").strip()
        match = next((a for a in apps if a.lower() == want), None) or next(
            (a for a in apps if want in a.lower() or a.lower() in want), None)
        if not match:
            raise RokuError(f"no app called {name!r} on the TV")
        self.post(f"launch/{apps[match]}")
        return match

    def type_text(self, text):
        for ch in text:
            self.post("keypress/Lit_" + urllib.parse.quote(ch, safe=""))

    def search(self, query, launch=False):
        q = urllib.parse.urlencode({"keyword": query, **({"launch": "true"} if launch else {})})
        self.post(f"search/browse?{q}")
