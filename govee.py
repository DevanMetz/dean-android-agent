"""Govee light control for Dean.

Two transports:
  * LAN API (UDP, ~50 ms, no cloud): for models with "LAN Control" switched on
    in the Govee Home app. Commands go to <ip>:4003; replies come back on 4002.
  * Cloud API (HTTPS, ~0.5-1 s): for everything else; needs GOVEE_API_KEY
    (Govee Home app -> Profile -> gear -> Apply for API Key).

Lights are listed in ~/assistant/lights.json:
  [{"name": "bedroom light", "aliases": ["my room", "ceiling"], "lan_ip": "192.168.1.204"},
   {"name": "living room lamp", "sku": "H1401", "device": "AA:BB:..."}]
Entries with "sku" + "device" use the cloud; entries with "lan_ip" use the LAN.
With an API key, cloud devices missing from the file are added automatically.
"""

import json
import os
import socket
import time
import uuid
from pathlib import Path

import httpx

LIGHTS_FILE = Path("/data/data/com.termux/files/home/assistant/lights.json")
CLOUD = "https://openapi.api.govee.com/router/api/v1"

COLORS = {
    "red": (255, 0, 0), "orange": (255, 110, 0), "yellow": (255, 200, 0), "green": (0, 255, 0),
    "teal": (0, 200, 160), "cyan": (0, 255, 255), "blue": (0, 0, 255), "purple": (170, 0, 255),
    "violet": (140, 60, 255), "magenta": (255, 0, 255), "pink": (255, 60, 150),
    "white": (255, 255, 255),
}


def parse_color(color):
    """'blue', '#00ff88' or 'ff8800' -> (r, g, b)."""
    c = color.strip().lower()
    if c in COLORS:
        return COLORS[c]
    c = c.lstrip("#")
    if len(c) == 6:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    raise ValueError(f"unknown color {color!r}; use a common name or hex like #ff8800")


class Govee:
    def __init__(self):
        self.key = os.environ.get("GOVEE_API_KEY", "")
        self.http = httpx.Client(timeout=10, headers={"Govee-API-Key": self.key})
        self.lights = json.loads(LIGHTS_FILE.read_text()) if LIGHTS_FILE.exists() else []
        self._synced = False

    # ----- inventory -----

    def sync_cloud(self):
        """Add cloud devices (name, sku, id) the file doesn't list yet."""
        if self._synced or not self.key:
            return
        self._synced = True
        r = self.http.get(f"{CLOUD}/user/devices")
        r.raise_for_status()
        known = {l.get("device") for l in self.lights}
        added = False
        for d in r.json().get("data", []):
            if not d.get("type", "").endswith("light") or d["device"] in known:
                continue
            # a LAN-only entry of the same model gets the cloud id instead of a duplicate
            lan_twin = next((l for l in self.lights if l.get("lan_ip") and not l.get("device")
                             and l.get("sku") == d["sku"]), None)
            if lan_twin:
                lan_twin["device"] = d["device"]
            else:
                self.lights.append({"name": d.get("deviceName") or d["sku"],
                                    "sku": d["sku"], "device": d["device"]})
            added = True
        if added:
            LIGHTS_FILE.write_text(json.dumps(self.lights, indent=1))

    def names(self):
        try:
            self.sync_cloud()
        except Exception:
            pass
        return [l["name"] for l in self.lights]

    def find(self, target):
        """Lights matching a name, alias or room word; 'all' matches everything."""
        self.names()
        t = (target or "all").lower().strip()
        if t in ("all", "everything", "every light", "all lights", "lights"):
            return list(self.lights)
        hits = [l for l in self.lights
                if t in l["name"].lower() or any(t in a.lower() or a.lower() in t
                                                 for a in l.get("aliases", []))]
        if not hits:  # match on any word, e.g. "living room" -> "living room bulb"
            words = [w for w in t.split() if len(w) > 2 and w not in ("light", "lights", "the")]
            hits = [l for l in self.lights
                    if any(w in (l["name"] + " " + " ".join(l.get("aliases", []))).lower()
                           for w in words)]
        return hits

    # ----- LAN -----

    @staticmethod
    def lan_send(ip, cmd, data):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (ip, 4003))

    @staticmethod
    def lan_status(ip, timeout=1.5):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rx.bind(("", 4002))
            rx.settimeout(timeout)
            Govee.lan_send(ip, "devStatus", {})
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = rx.recvfrom(4096)
                except socket.timeout:
                    break
                if addr[0] == ip:
                    return json.loads(data)["msg"]["data"]
        return None

    # ----- cloud -----

    def cloud_control(self, light, cap_type, instance, value):
        r = self.http.post(f"{CLOUD}/device/control", json={
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": light["sku"], "device": light["device"],
                        "capability": {"type": cap_type, "instance": instance, "value": value}}})
        r.raise_for_status()
        body = r.json()
        if body.get("code") not in (200, None):
            raise RuntimeError(body.get("msg") or body)

    def cloud_state(self, light):
        r = self.http.post(f"{CLOUD}/device/state", json={
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": light["sku"], "device": light["device"]}})
        r.raise_for_status()
        caps = {c["instance"]: c["state"]["value"]
                for c in r.json().get("payload", {}).get("capabilities", [])}
        out = {"on": caps.get("powerSwitch") == 1, "brightness": caps.get("brightness")}
        if caps.get("colorRgb"):
            v = caps["colorRgb"]
            out["color"] = f"#{v:06x}"
        if caps.get("colorTemperatureK"):
            out["kelvin"] = caps["colorTemperatureK"]
        return out

    # ----- one light -----

    def apply(self, light, on=None, brightness=None, color=None, kelvin=None):
        lan = light.get("lan_ip")
        if lan:
            if on is not None:
                self.lan_send(lan, "turn", {"value": 1 if on else 0})
            if brightness is not None:
                self.lan_send(lan, "brightness", {"value": int(brightness)})
            if color is not None:
                r, g, b = color
                self.lan_send(lan, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
            if kelvin is not None:
                self.lan_send(lan, "colorwc", {"color": {"r": 255, "g": 255, "b": 255},
                                               "colorTemInKelvin": int(kelvin)})
            return
        if not (self.key and light.get("device")):
            raise RuntimeError(f"{light['name']} needs a Govee API key or LAN control")
        if on is not None:
            self.cloud_control(light, "devices.capabilities.on_off", "powerSwitch", 1 if on else 0)
        if brightness is not None:
            self.cloud_control(light, "devices.capabilities.range", "brightness", int(brightness))
        if color is not None:
            r, g, b = color
            self.cloud_control(light, "devices.capabilities.color_setting", "colorRgb",
                               (r << 16) + (g << 8) + b)
        if kelvin is not None:
            self.cloud_control(light, "devices.capabilities.color_setting", "colorTemperatureK",
                               int(kelvin))

    def status(self, light):
        if light.get("lan_ip"):
            s = self.lan_status(light["lan_ip"])
            if s is None:
                return {"error": "no reply on the local network"}
            out = {"on": s.get("onOff") == 1, "brightness": s.get("brightness")}
            if s.get("colorTemInKelvin"):
                out["kelvin"] = s["colorTemInKelvin"]
            c = s.get("color") or {}
            out["color"] = "#%02x%02x%02x" % (c.get("r", 0), c.get("g", 0), c.get("b", 0))
            return out
        if self.key and light.get("device"):
            return self.cloud_state(light)
        return {"error": "needs a Govee API key"}
