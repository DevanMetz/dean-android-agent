"""Tools Dean can call: the tablet's hardware (via Termux:API) plus timers,
memory and web search. Each tool is a plain function returning something
JSON-serialisable; TOOLS holds the schemas sent to the model."""

import base64
import io
import json
import inspect
import math
import os
import smtplib
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import httpx
from PIL import Image

import briefing
from govee import Govee, parse_color
from roku import INPUTS, KEYS, Roku, RokuError
from scheduler import delete_routine, routines, save_routine

TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
DATA = Path("/data/data/com.termux/files/home/assistant")
MEMORY_FILE = DATA / "memory.json"
PLACE_FILE = DATA / "place.json"
PHOTO = DATA / "snap.jpg"
SENSORS_FILE = DATA / "sensors.json"  # {"GVH5075_ABCD": "balcony"}
SENSOR_APP = "http://127.0.0.1:8765/"  # android/dean-sensors, running on this tablet
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
        self.lock = threading.Lock()
        self.scheduler = None  # set by dean.py once speech is ready
        self.govee = Govee()
        self._roku = None

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

    # ----- reminders, alarms, timers, routines (see scheduler.py) -----

    @staticmethod
    def _when(when=None, in_minutes=None):
        if in_minutes is not None:
            return datetime.now() + timedelta(minutes=float(in_minutes))
        if when:
            return datetime.fromisoformat(when.strip().replace("Z", "").replace(" ", "T"))
        raise ValueError("give 'when' (local time, e.g. 2026-10-01T19:00) or 'in_minutes'")

    @staticmethod
    def _where(ctx, announce_at_home):
        """Reminders set by text go back to that chat; spoken at home unless set remotely."""
        ctx = ctx or {}
        texted = ctx.get("channel") == "telegram"
        speak = (not texted) if announce_at_home is None else bool(announce_at_home)
        return {"notify": ctx.get("chat") if texted else None, "speak": speak}

    def set_reminder(self, text, when=None, in_minutes=None, repeat="none",
                     announce_at_home=None, _ctx=None):
        return self.scheduler.add("reminder", self._when(when, in_minutes), text=text,
                                  repeat=repeat, **self._where(_ctx, announce_at_home))

    def set_timer(self, minutes, label="timer", _ctx=None):
        return self.scheduler.add("timer", self._when(in_minutes=minutes), text=label,
                                  **self._where(_ctx, None))

    def set_alarm(self, when, repeat="none", label="alarm", routine=None, _ctx=None):
        if routine and routine.lower().strip() not in routines():
            return {"error": f"no routine called {routine!r}", "routines": list(routines())}
        return self.scheduler.add("alarm", self._when(when), text=label, repeat=repeat,
                                  routine=routine, notify=self._where(_ctx, None)["notify"])

    def schedule_routine(self, routine, when, repeat="none", _ctx=None):
        if routine.lower().strip() not in routines():
            return {"error": f"no routine called {routine!r}", "routines": list(routines())}
        return self.scheduler.add("routine", self._when(when), text=routine, repeat=repeat,
                                  routine=routine, notify=self._where(_ctx, None)["notify"])

    def list_scheduled(self):
        return self.scheduler.upcoming() or {"note": "nothing scheduled"}

    def cancel_scheduled(self, match):
        gone = self.scheduler.cancel(match)
        return {"cancelled": gone} if gone else {"error": f"nothing scheduled matches {match!r}"}

    def create_routine(self, name, steps):
        save_routine(name, steps)
        return {"saved": name, "steps": steps}

    def run_routine(self, name):
        r = routines()
        steps = r.get(name.lower().strip())
        if steps is None:
            return {"error": f"no routine called {name!r}", "routines": list(r)}
        return {"routine": name, "steps": steps,
                "instructions": "Carry out each step now with your tools, then confirm briefly."}

    def list_routines(self):
        return routines() or {"note": "no routines yet"}

    def remove_routine(self, name):
        return {"deleted": delete_routine(name)}

    def announce(self, text):
        """Say something out loud on the tablet at home."""
        threading.Thread(target=self.say, args=(text,), daemon=True).start()
        return {"announced": text}

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

    # ----- lights -----

    def lights(self, target="all", power=None, brightness=None, color=None, kelvin=None):
        hits = self.govee.find(target)
        if not hits:
            return {"error": f"no light matches {target!r}", "lights": self.govee.names()}
        rgb = parse_color(color) if color else None
        if brightness is not None and brightness <= 0:
            power, brightness = "off", None
        change = power is not None or brightness is not None or rgb or kelvin
        on = None if power is None else power == "on"
        if change and on is None and (brightness or rgb or kelvin):
            on = True  # "make it blue" implies turning it on

        def one(light):
            try:
                if not change:
                    return {"light": light["name"], **self.govee.status(light)}
                self.govee.apply(light, on=on, brightness=brightness, color=rgb, kelvin=kelvin)
                return {"light": light["name"], "done": True}
            except Exception as e:
                return {"light": light["name"], "error": f"{type(e).__name__}: {e}"}

        with ThreadPoolExecutor(len(hits)) as pool:
            return list(pool.map(one, hits))

    # ----- finding phones -----

    def find_phone(self, phone, stop=False):
        p = phone.lower()
        if any(w in p for w in ("pixel", "android", "google")):
            topic = os.environ.get("DEAN_PIXEL_TOPIC")
            if not topic:
                return {"error": "the Pixel isn't set up yet (DEAN_PIXEL_TOPIC missing)"}
            # the Dean Finder app on the Pixel listens on this private ntfy channel
            r = httpx.post(f"https://ntfy.sh/{topic}", content="stop" if stop else "ring", timeout=10)
            r.raise_for_status()
            return {"phone": "Pixel", "ringing": not stop,
                    "note": "" if stop else "rings at full alarm volume for up to a minute"}
        if any(w in p for w in ("iphone", "apple", "ios")):
            if stop:
                return {"note": "the iPhone stops by itself when its shortcut finishes"}
            return self.ring_iphone()
        return {"error": f"unknown phone {phone!r}; expected the Pixel or the iPhone"}

    def ring_iphone(self):
        """Email the iPhone; a Shortcuts automation on it ("when I get an email with
        this subject") turns the volume up and makes noise, even on silent."""
        need = ("DEAN_SMTP_HOST", "DEAN_SMTP_USER", "DEAN_SMTP_PASSWORD", "DEAN_IPHONE_EMAIL")
        missing = [k for k in need if not os.environ.get(k)]
        if missing:
            return {"error": "the iPhone isn't set up yet (missing " + ", ".join(missing) + ")"}
        msg = EmailMessage()
        msg["From"] = os.environ["DEAN_SMTP_USER"]
        msg["To"] = os.environ["DEAN_IPHONE_EMAIL"]
        msg["Subject"] = os.environ.get("DEAN_IPHONE_SUBJECT", "Dean find my iPhone")
        msg.set_content("Dean is looking for your iPhone.")
        host = os.environ["DEAN_SMTP_HOST"]
        with smtplib.SMTP(host, int(os.environ.get("DEAN_SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            s.login(os.environ["DEAN_SMTP_USER"], os.environ["DEAN_SMTP_PASSWORD"])
            s.send_message(msg)
        return {"phone": "iPhone", "ringing": True,
                "note": "the iPhone gets the email in a few seconds, then its shortcut plays sound"}

    # ----- calendar & news (briefing.py) -----

    def calendar_events(self, day="today", days=1):
        return briefing.events(briefing.parse_day(day), max(1, min(int(days), 14)))

    def news_headlines(self, topic=None, count=5):
        return briefing.headlines(topic, max(1, min(int(count), 10)))

    # ----- OpenRouter spending -----

    def ai_spending(self):
        r = self.http.get("https://openrouter.ai/api/v1/key", timeout=10)
        r.raise_for_status()
        d = r.json().get("data", {})
        money = lambda v: None if v is None else f"${v:,.2f}"  # noqa: E731
        out = {"today": money(d.get("usage_daily")), "this_week": money(d.get("usage_weekly")),
               "this_month": money(d.get("usage_monthly")), "all_time": money(d.get("usage")),
               "spending_limit": money(d.get("limit")),
               "left_before_limit": money(d.get("limit_remaining"))}
        return {k: v for k, v in out.items() if v is not None}

    def restart_sensor_app(self):
        """Used by the health check when the Dean Sensors app stops answering."""
        return _run(["am", "broadcast", "-n", "com.dean.sensors/.BootReceiver",
                     "-a", "com.dean.sensors.START"], 20, None)

    # ----- Roku TV -----

    def tv(self, action, app=None, text=None, times=1):
        self._roku = self._roku or Roku()
        tv = self._roku
        try:
            if action == "status":
                return tv.status()
            if action == "apps":
                return {"apps": sorted(tv.apps())}
            if action == "launch":
                return {"opened": tv.launch(app or text or "")}
            if action == "search":
                tv.search(text or app or "")
                return {"searched_for": text or app}
            if action == "type":
                tv.type_text(text or "")
                return {"typed": text}
            if action == "input":
                key = INPUTS.get((text or "").lower().replace(" ", ""))
                if not key:
                    return {"error": f"unknown input {text!r}", "inputs": list(INPUTS)}
                tv.press(key)
                return {"input": text}
            key = KEYS.get(action)
            if not key:
                return {"error": f"unknown action {action!r}"}
            tv.press(key, times)
            return {"done": action, "times": times}
        except RokuError as e:
            return {"error": str(e)}

    # ----- Bluetooth thermometers (via the Dean Sensors app) -----

    def climate_sensors(self):
        try:
            data = httpx.get(SENSOR_APP, timeout=3).json()
        except Exception:
            return {"error": "the Dean Sensors app isn't running on the tablet"}
        names = json.loads(SENSORS_FILE.read_text()) if SENSORS_FILE.exists() else {}
        sensors = data.get("sensors", [])
        default = os.environ.get("DEAN_SENSOR_DEFAULT_NAME", "balcony")
        out = []
        for s in sensors:
            name = names.get(s["name"]) or (default if len(sensors) == 1 else s["name"])
            age = int(time.time() - s["time"])
            us = (self.place().get("country") or "United States") == "United States"
            out.append({
                "name": name,
                "temperature": f"{s['temp_f']}°F" if us else f"{s['temp_c']}°C",
                "humidity": f"{s['humidity']}%",
                "battery": f"{s['battery']}%",
                "last_heard": "just now" if age < 120 else f"{age // 60} minutes ago",
            })
        if not out:
            return {"error": "no thermometer heard yet; it may be out of the tablet's Bluetooth "
                             "range", "bluetooth_broadcasts_heard": data.get("adverts_heard")}
        return out

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

    def call(self, name, args, ctx=None):
        fn = getattr(self, name, None)
        if name not in TOOL_NAMES or fn is None:
            return {"error": f"unknown tool {name}"}
        args = {k: v for k, v in args.items() if not k.startswith("_")}
        if "_ctx" in inspect.signature(fn).parameters:
            args["_ctx"] = ctx
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
    _tool("lights", "Control or check the Govee smart lights. Leave power, brightness, color "
          "and kelvin all out to just get their current state.",
          {"target": {"type": "string", "description": "A light or room name, or 'all'."},
           "power": {"type": "string", "enum": ["on", "off"]},
           "brightness": {"type": "number", "minimum": 0, "maximum": 100,
                          "description": "Percent."},
           "color": {"type": "string",
                     "description": "Color name (red, orange, yellow, green, teal, blue, purple, "
                                    "pink, white...) or hex like #ff8800."},
           "kelvin": {"type": "number", "minimum": 2000, "maximum": 9000,
                      "description": "White color temperature: ~2700 warm, ~4000 neutral, "
                                     "~6500 daylight."}},
          ["target"]),
    _tool("find_phone", "Make the person's phone ring loudly so they can find it (works even "
          "when it's on silent). Also stops the Pixel ringing.",
          {"phone": {"type": "string", "enum": ["pixel", "iphone"]},
           "stop": {"type": "boolean", "description": "Stop ringing instead."}},
          ["phone"]),
    _tool("calendar_events", "Events from the household's calendars for a day (or several).",
          {"day": {"type": "string",
                   "description": "'today', 'tomorrow', a weekday name, or YYYY-MM-DD."},
           "days": {"type": "integer", "minimum": 1, "maximum": 14}}),
    _tool("news_headlines", "Current news headlines, top stories or about a topic. Faster than "
          "web_search for 'what's in the news'.",
          {"topic": {"type": "string", "description": "Optional, e.g. 'Milwaukee Bucks'."},
           "count": {"type": "integer", "minimum": 1, "maximum": 10}}),
    _tool("ai_spending", "How much Dean's AI usage has cost (today, this week, this month, all "
          "time) and how much is left before the spending limit."),
    _tool("tv", "Control the Roku TV: power, volume, open apps (Netflix, YouTube...), play/pause, "
          "navigate, search for a show, switch inputs, or check what's on.",
          {"action": {"type": "string", "enum": [
              "status", "on", "off", "volume_up", "volume_down", "mute", "launch", "apps",
              "search", "type", "play_pause", "rewind", "fast_forward", "replay", "home", "back",
              "select", "up", "down", "left", "right", "channel_up", "channel_down", "input", "info"]},
           "app": {"type": "string", "description": "App name for 'launch', e.g. Netflix."},
           "text": {"type": "string",
                    "description": "Show/movie for 'search', text for 'type', or input name for "
                                   "'input' (hdmi1-4, antenna)."},
           "times": {"type": "integer", "minimum": 1, "maximum": 30,
                     "description": "Repeat count, e.g. volume_up 5 times for 'a lot louder'."}},
          ["action"]),
    _tool("climate_sensors", "Temperature, humidity and battery from the household's Govee "
          "Bluetooth thermometers (e.g. the one on the balcony). Use for 'how warm is it on the "
          "balcony'; use get_weather for the forecast.",),
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
          {"minutes": {"type": "number"},
           "label": {"type": "string", "description": "Short name, e.g. 'pasta'."}},
          ["minutes"]),
    _tool("set_reminder", "Remind the person about something at a time (optionally repeating). "
          "Reminders set by voice are spoken at home; set by text, they're texted back.",
          {"text": {"type": "string", "description": "What to remind them, e.g. 'take out the trash'."},
           "when": {"type": "string", "description": "Local date and time, e.g. 2026-10-01T19:00. Work it out from the current local time you were given."},
           "in_minutes": {"type": "number", "description": "Alternative to 'when'."},
           "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekends", "weekly"]},
           "announce_at_home": {"type": "boolean",
                                "description": "Also say it out loud at home (for text requests)."}},
          ["text"]),
    _tool("set_alarm", "Wake-up alarm: rings and speaks at home until someone says 'hey Dean' "
          "(or a few minutes pass). Can run a routine when it goes off.",
          {"when": {"type": "string", "description": "Local date and time, e.g. 2026-10-01T19:00. Work it out from the current local time you were given."}, "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekends", "weekly"]},
           "label": {"type": "string"},
           "routine": {"type": "string", "description": "Name of a routine to run, e.g. 'good morning'."}},
          ["when"]),
    _tool("schedule_routine", "Run a saved routine automatically at a time, e.g. 'good night' every "
          "day at 11 PM.",
          {"routine": {"type": "string"}, "when": {"type": "string", "description": "Local date and time, e.g. 2026-10-01T19:00. Work it out from the current local time you were given."}, "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekends", "weekly"]}},
          ["routine", "when"]),
    _tool("list_scheduled", "List upcoming reminders, alarms, timers and scheduled routines."),
    _tool("cancel_scheduled", "Cancel scheduled items whose text, routine, kind ('timer', 'alarm', "
          "'reminder') or id matches; 'all' cancels everything.",
          {"match": {"type": "string"}}, ["match"]),
    _tool("create_routine", "Save (or replace) a named routine: plain-English steps you'll carry out "
          "with your tools when it runs, e.g. 'turn off all lights; set an alarm for 7 AM weekdays'.",
          {"name": {"type": "string"}, "steps": {"type": "string"}}, ["name", "steps"]),
    _tool("run_routine", "Get a saved routine's steps so you can carry them out now.",
          {"name": {"type": "string"}}, ["name"]),
    _tool("list_routines", "List saved routines and their steps."),
    _tool("remove_routine", "Delete a saved routine.", {"name": {"type": "string"}}, ["name"]),
    _tool("announce", "Say something out loud on the tablet at home (for when the person is "
          "texting from elsewhere).", {"text": {"type": "string"}}, ["text"]),
    _tool("remember", "Save a lasting fact about the household or the person's preferences "
          "(names, birthdays, likes, routines). Use when asked to remember something.",
          {"fact": {"type": "string"}}, ["fact"]),
    _tool("forget", "Delete remembered facts containing this text.",
          {"about": {"type": "string"}}, ["about"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
