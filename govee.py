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
import threading
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
        if not hits and len(self.lights) == 1:
            hits = list(self.lights)  # only one light: any light request means that one
        return hits

    # ----- LAN -----

    @staticmethod
    def lan_send(ip, cmd, data):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (ip, 4003))

    # every light replies to port 4002, which only one socket can receive on at a time
    _status_lock = threading.Lock()

    @staticmethod
    def lan_status(ip, timeout=1.5):
        with Govee._status_lock, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
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

    @staticmethod
    def lan_scan(ips, timeout=2.0):
        """Ask each address who it is: {ip: {"device": id, "sku": model}}. Unicast to 4001
        works here, while Android tends to drop replies to the multicast scan."""
        found = {}
        with Govee._status_lock, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rx.bind(("", 4002))
            rx.settimeout(0.3)
            msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                for ip in ips:
                    tx.sendto(msg, (ip, 4001))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = rx.recvfrom(4096)
                    d = json.loads(data)["msg"]["data"]
                    if d.get("device"):
                        found[addr[0]] = {"device": d["device"], "sku": d.get("sku")}
                except socket.timeout:
                    pass
                except (ValueError, KeyError):
                    pass
        return found

    def heal(self):
        """Learn hardware ids, and follow lights whose IP address changed. Returns a list
        of what changed (empty when all is well)."""
        lan = [l for l in self.lights if l.get("lan_ip")]
        if not lan:
            return []
        changes = []
        seen = self.lan_scan([l["lan_ip"] for l in lan])
        for l in lan:
            hit = seen.get(l["lan_ip"])
            if hit and not l.get("device"):
                l["device"], l["sku"] = hit["device"], hit.get("sku") or l.get("sku")
                changes.append(f"learned the id of {l['name']}")
        missing = [l for l in lan if l.get("device") and l["lan_ip"] not in seen]
        if missing and time.time() - getattr(self, "_last_sweep", 0) > 1800:
            self._last_sweep = time.time()
            subnet = lan[0]["lan_ip"].rsplit(".", 1)[0]
            everyone = self.lan_scan([f"{subnet}.{i}" for i in range(1, 255)], timeout=3.0)
            where = {v["device"]: ip for ip, v in everyone.items()}
            for l in missing:
                if l["device"] in where and where[l["device"]] != l["lan_ip"]:
                    changes.append(f"{l['name']} moved from {l['lan_ip']} to {where[l['device']]}")
                    l["lan_ip"] = where[l["device"]]
        if changes:
            LIGHTS_FILE.write_text(json.dumps(self.lights, indent=1))
        return changes

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

    @staticmethod
    def matches(s, on=None, brightness=None, color=None, kelvin=None):
        """Does a LAN status reply show the requested change?"""
        if on is not None and (s.get("onOff") == 1) != on:
            return False
        if on is False:
            return True  # off: nothing else to compare
        if brightness is not None and abs((s.get("brightness") or 0) - brightness) > 2:
            return False
        if color is not None:
            c = s.get("color") or {}
            if any(abs(c.get(k, 0) - v) > 8 for k, v in zip("rgb", color)):
                return False
        if kelvin is not None and abs((s.get("colorTemInKelvin") or 0) - kelvin) > 150:
            return False
        return True

    def apply_and_confirm(self, light, on=None, brightness=None, color=None, kelvin=None):
        """Send the change, then read the light back; re-send once if it didn't take.
        LAN commands are fire-and-forget UDP, so without this a light that's switched
        off at the wall would be reported as changed."""
        name = light["name"]
        if not light.get("lan_ip"):
            self.apply(light, on, brightness, color, kelvin)  # the cloud API confirms itself
            return {"light": name, "done": True}
        for attempt in range(2):
            self.apply(light, on, brightness, color, kelvin)
            time.sleep(0.6)
            s = self.lan_status(light["lan_ip"])
            if s is None:
                continue
            if self.matches(s, on, brightness, color, kelvin):
                return {"light": name, "done": True, "confirmed": True}
        if s is None:
            return {"light": name, "error": f"{name} isn't responding on the network; it may be "
                                             "switched off at the wall"}
        return {"light": name, "error": f"{name} didn't change",
                "it_reports": {"on": s.get("onOff") == 1, "brightness": s.get("brightness")}}

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
