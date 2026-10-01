"""Tools Dean can call: the tablet's hardware (via Termux:API) plus timers,
memory and web search. Each tool is a plain function returning something
JSON-serialisable; TOOLS holds the schemas sent to the model."""

import base64
import io
import json
import math
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image

TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
DATA = Path("/data/data/com.termux/files/home/assistant")
MEMORY_FILE = DATA / "memory.json"
PLACE_FILE = DATA / "place.json"
PHOTO = DATA / "snap.jpg"
BRIDGE = DATA / "bridge.sock"  # termux/bridge.py, running natively in Termux
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def _run(args, timeout, stdin):
    """Run a Termux:API command, via the native bridge when it's up (~0.35 s)
    instead of starting it under proot (~2.5 s)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout + 5)
            s.connect(str(BRIDGE))
            s.sendall(json.dumps({"args": list(args), "stdin": stdin, "timeout": timeout}).encode() + b"\n")
            reply = json.loads(s.makefile().readline())
        if reply.get("error"):
            raise RuntimeError(reply["error"])
        return reply["stdout"]
    except (FileNotFoundError, ConnectionRefusedError):
        r = subprocess.run([f"{TERMUX_BIN}/{args[0]}", *args[1:]], capture_output=True,
                           text=True, timeout=timeout, input=stdin)
        return r.stdout


def termux(*args, timeout=30, stdin=None):
    out = _run(args, timeout, stdin).strip()
    try:
        result = json.loads(out) if out else {}
    except json.JSONDecodeError:
        return out
    # Termux:API reports failures (e.g. missing permissions) as {"error": ...} with exit 0
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(result["error"])
    return result


class Toolbox:
    def __init__(self, http, models, vision_model, say, chime):
        self.http = http  # httpx.Client with the OpenRouter key
        self.models = models  # preferred model first, then fallbacks
        self.vision_model = vision_model  # used for camera questions
        self.say = say  # speak(text) - used by timers
        self.chime = chime
        self.timers = {}
        self.lock = threading.Lock()

    # ----- location -----

    def place(self, refresh=False):
        """Cached location; refreshed at most once a day."""
        if not refresh and PLACE_FILE.exists():
            p = json.loads(PLACE_FILE.read_text())
            if time.time() - p.get("at", 0) < 86400:
                return p
        fix = termux("termux-location", "-p", "network", "-r", "once", timeout=45)
        if not isinstance(fix, dict) or "latitude" not in fix:
            return json.loads(PLACE_FILE.read_text()) if PLACE_FILE.exists() else {}
        lat, lon = fix["latitude"], fix["longitude"]
        p = {"lat": round(lat, 3), "lon": round(lon, 3), "at": time.time()}
        try:  # reverse-geocode once with OpenStreetMap
            a = httpx.get("https://nominatim.openstreetmap.org/reverse",
                          params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14},
                          headers={"User-Agent": "dean-wall-assistant/1.0"}, timeout=15
                          ).json().get("address", {})
            p.update(city=a.get("city") or a.get("town") or a.get("village") or a.get("suburb"),
                     county=a.get("county"), state=a.get("state"),
                     country=a.get("country"), postcode=a.get("postcode"))
        except Exception:
            pass
        PLACE_FILE.write_text(json.dumps(p))
        return p

    def place_line(self):
        p = self.place()
        if not p:
            return ""
        where = ", ".join(x for x in (p.get("city"), p.get("state"), p.get("country")) if x)
        return f"The tablet is located in {where} (about {p['lat']}, {p['lon']})." if where else ""

    def get_location(self, refresh=False):
        return {k: v for k, v in self.place(refresh).items() if k != "at"}

    # ----- camera -----

    def look(self, question, camera="front"):
        self.chime()  # audible cue whenever the camera is used
        PHOTO.unlink(missing_ok=True)
        termux("termux-camera-photo", "-c", "1" if camera == "front" else "0", str(PHOTO), timeout=30)
        if not PHOTO.exists():
            return {"error": "camera did not return a photo"}
        img = Image.open(PHOTO)
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
        PHOTO.unlink(missing_ok=True)  # don't keep photos around
        b64 = base64.b64encode(buf.getvalue()).decode()
        r = self.http.post(OPENROUTER, json={
            "model": self.vision_model, "max_tokens": 1500, "reasoning": {"effort": "low", "exclude": True},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "This photo was just taken by a wall-mounted tablet's "
                 f"{camera} camera. Answer briefly and concretely: {question}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
            timeout=90)
        r.raise_for_status()
        return {"camera": camera, "answer": r.json()["choices"][0]["message"]["content"]}

    # ----- sensors & status -----

    def read_sensors(self):
        s = termux("termux-sensor", "-s", "sy3135_als,icm4n607_acc,aw963xx_sar", "-n", "1", timeout=20)
        out = {}
        if isinstance(s, dict):
            if "sy3135_als" in s:
                lux = s["sy3135_als"]["values"][0]
                out["light_lux"] = lux
                out["light"] = ("dark" if lux < 10 else "dim" if lux < 80 else
                                "normal indoor" if lux < 500 else "bright" if lux < 5000 else "sunlight")
            if "icm4n607_acc" in s:
                x, y, z = s["icm4n607_acc"]["values"][:3]
                g = math.sqrt(x * x + y * y + z * z) or 1
                out["orientation"] = ("lying flat" if abs(z) / g > 0.8 else
                                      "upright (portrait)" if abs(y) / g > 0.7 else
                                      "upright (landscape)" if abs(x) / g > 0.7 else "tilted")
            if "aw963xx_sar" in s:
                out["something_very_close_to_screen"] = any(v for v in s["aw963xx_sar"]["values"])
        return out

    def device_status(self):
        with ThreadPoolExecutor(3) as pool:
            b, w, v = pool.map(termux, ("termux-battery-status", "termux-wifi-connectioninfo",
                                        "termux-volume"))
        music = next((x for x in v if x.get("stream") == "music"), {}) if isinstance(v, list) else {}
        return {
            "battery_percent": b.get("percentage"), "charging": b.get("status"),
            "plugged": b.get("plugged"), "battery_temp_c": b.get("temperature"),
            "battery_health": b.get("health"),
            "wifi_network": w.get("ssid"), "wifi_signal_dbm": w.get("rssi"),
            "volume_percent": round(100 * music.get("volume", 0) / max(music.get("max_volume", 15), 1)),
            "time": datetime.now().strftime("%A %I:%M %p"),
        }

    # ----- controls -----

    def set_volume(self, percent):
        level = round(max(0, min(100, percent)) / 100 * 15)
        termux("termux-volume", "music", str(level))
        return {"volume_percent": round(level / 15 * 100)}

    def set_brightness(self, percent=None, auto=False):
        if auto:
            termux("termux-brightness", "auto")
            return {"brightness": "auto"}
        termux("termux-brightness", str(round(max(0, min(100, percent)) / 100 * 255)))
        return {"brightness_percent": percent}

    def flashlight(self, on):
        termux("termux-torch", "on" if on else "off")
        return {"flashlight": "on" if on else "off"}

    # ----- timers -----

    def set_timer(self, minutes, label="timer"):
        due = datetime.now() + timedelta(minutes=minutes)
        tid = f"{label}-{due.strftime('%H%M%S')}"

        def fire():
            with self.lock:
                self.timers.pop(tid, None)
            for _ in range(3):
                self.chime()
            self.say(f"Your {label} is done." if label != "timer" else "Your timer is done.")

        t = threading.Timer(minutes * 60, fire)
        t.daemon = True
        with self.lock:
            self.timers[tid] = (t, due, label)
        t.start()
        return {"set": label, "goes_off_at": due.strftime("%I:%M:%S %p")}

    def list_timers(self):
        with self.lock:
            return [{"label": l, "remaining_seconds": int((d - datetime.now()).total_seconds())}
                    for _, d, l in self.timers.values()]

    def cancel_timer(self, label):
        with self.lock:
            hits = [k for k, (_, _, l) in self.timers.items() if label.lower() in l.lower()]
            for k in hits:
                self.timers.pop(k)[0].cancel()
        return {"cancelled": len(hits)}

    # ----- memory -----

    def memories(self):
        return json.loads(MEMORY_FILE.read_text()) if MEMORY_FILE.exists() else []

    def remember(self, fact):
        m = self.memories()
        if fact not in m:
            m.append(fact)
            MEMORY_FILE.write_text(json.dumps(m, indent=1))
        return {"remembered": fact, "total": len(m)}

    def forget(self, about):
        m = self.memories()
        keep = [f for f in m if about.lower() not in f.lower()]
        MEMORY_FILE.write_text(json.dumps(keep, indent=1))
        return {"forgot": len(m) - len(keep)}

    # ----- weather -----

    WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
           48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
           56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
           65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
           73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
           82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
           95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}

    def get_weather(self, days=3):
        """Current conditions and daily forecast from Open-Meteo (free, no key)."""
        p = self.place()
        if not p:
            return {"error": "location unknown"}
        us = p.get("country") in (None, "United States")
        r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
            "latitude": p["lat"], "longitude": p["lon"], "timezone": "auto",
            "forecast_days": max(1, min(int(days), 7)),
            "temperature_unit": "fahrenheit" if us else "celsius",
            "wind_speed_unit": "mph" if us else "kmh",
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,precipitation_sum,wind_speed_10m_max,sunrise,sunset",
        })
        r.raise_for_status()
        d, c = r.json()["daily"], r.json()["current"]
        unit = "F" if us else "C"
        return {
            "place": p.get("city"),
            "now": {"temp": f"{c['temperature_2m']}{unit}", "feels_like": f"{c['apparent_temperature']}{unit}",
                    "conditions": self.WMO.get(c["weather_code"], "unknown"),
                    "wind": f"{c['wind_speed_10m']} {'mph' if us else 'km/h'}",
                    "humidity": f"{c['relative_humidity_2m']}%"},
            "days": [{"date": datetime.fromisoformat(day).strftime("%A %b %d"),
                      "conditions": self.WMO.get(d["weather_code"][i], "unknown"),
                      "high": f"{d['temperature_2m_max'][i]}{unit}", "low": f"{d['temperature_2m_min'][i]}{unit}",
                      "chance_of_precipitation": f"{d['precipitation_probability_max'][i]}%",
                      "precipitation": d["precipitation_sum"][i],
                      "max_wind": d["wind_speed_10m_max"][i],
                      "sunrise": d["sunrise"][i][-5:], "sunset": d["sunset"][i][-5:]}
                     for i, day in enumerate(d["time"])],
        }

    # ----- web -----

    def web_search(self, query):
        r = self.http.post(OPENROUTER, json={
            "models": self.models, "max_tokens": 1500,
            "reasoning": {"effort": "minimal", "exclude": True},
            "plugins": [{"id": "web", "max_results": 4}],
            "messages": [{"role": "user", "content":
                          f"Today is {datetime.now().strftime('%A, %B %d, %Y, %I:%M %p')}. "
                          f"{self.place_line()} Search the web and give a short factual answer "
                          f"(include specific numbers, names and times): {query}"}]}, timeout=60)
        r.raise_for_status()
        return {"result": r.json()["choices"][0]["message"]["content"]}

    # ----- dispatch -----

    def call(self, name, args):
        fn = getattr(self, name, None)
        if name not in TOOL_NAMES or fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except Exception as e:  # report failures to the model instead of crashing
            return {"error": f"{type(e).__name__}: {e}"}


def _tool(name, description, props=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}


TOOLS = [
    _tool("get_weather", "Weather at the tablet's location: current conditions plus a daily "
          "forecast (highs, lows, rain chance, wind, sunrise and sunset). Fast; use this for any "
          "local weather question instead of web_search.",
          {"days": {"type": "integer", "minimum": 1, "maximum": 7,
                    "description": "Days of forecast including today (default 3)."}}),
    _tool("web_search", "Search the web for current information: news, sports, business hours, "
          "prices, events, weather elsewhere, anything that may have changed recently.",
          {"query": {"type": "string"}}, ["query"]),
    _tool("get_location", "Get the tablet's current location (city, state, coordinates).",
          {"refresh": {"type": "boolean", "description": "Force a fresh location fix."}}),
    _tool("look", "Take a photo with the tablet camera and answer a question about it. Only use "
          "when the person asks you to look at or see something. The front camera faces the room.",
          {"question": {"type": "string", "description": "What to find out from the photo."},
           "camera": {"type": "string", "enum": ["front", "back"]}}, ["question"]),
    _tool("read_sensors", "Read the room light level, the tablet's orientation, and whether "
          "something is right up against the screen."),
    _tool("device_status", "Battery level and charging state, battery temperature, Wi-Fi "
          "network and signal, current volume."),
    _tool("set_volume", "Set the speaker volume.",
          {"percent": {"type": "number", "minimum": 0, "maximum": 100}}, ["percent"]),
    _tool("set_brightness", "Set screen brightness, or switch to automatic brightness.",
          {"percent": {"type": "number", "minimum": 0, "maximum": 100},
           "auto": {"type": "boolean"}}),
    _tool("flashlight", "Turn the camera flashlight on or off.",
          {"on": {"type": "boolean"}}, ["on"]),
    _tool("set_timer", "Start a countdown timer that chimes and speaks when done.",
          {"minutes": {"type": "number"}, "label": {"type": "string",
                                                     "description": "Short name, e.g. 'pasta'."}},
          ["minutes"]),
    _tool("list_timers", "List running timers and time remaining."),
    _tool("cancel_timer", "Cancel timers whose label matches.",
          {"label": {"type": "string"}}, ["label"]),
    _tool("remember", "Save a lasting fact about the household or the person's preferences "
          "(names, birthdays, likes, routines). Use when asked to remember something.",
          {"fact": {"type": "string"}}, ["fact"]),
    _tool("forget", "Delete remembered facts containing this text.",
          {"about": {"type": "string"}}, ["about"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
