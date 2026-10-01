"""Full-screen wall display for the Termux window: big clock, date, weather,
lights, what's coming up, and the recent conversation, redrawn every second.

Weather and lights refresh in the background so drawing never waits on the network.
"""

import re
import shutil
import sys
import textwrap
import threading
import time
from collections import deque
from datetime import datetime

# 3x5 pixel font; each pixel is drawn two characters wide
FONT = {
    "0": ["###", "# #", "# #", "# #", "###"], "1": [" # ", "## ", " # ", " # ", "###"],
    "2": ["###", "  #", "###", "#  ", "###"], "3": ["###", "  #", " ##", "  #", "###"],
    "4": ["# #", "# #", "###", "  #", "  #"], "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"], "7": ["###", "  #", " # ", " # ", " # "],
    "8": ["###", "# #", "###", "# #", "###"], "9": ["###", "# #", "###", "  #", "###"],
    ":": [" ", "#", " ", "#", " "],
}
C = {"dim": "\033[2m", "bold": "\033[1m", "cyan": "\033[36m", "green": "\033[32m",
     "yellow": "\033[33m", "red": "\033[31m", "blue": "\033[34m", "rev": "\033[7m", "off": "\033[0m"}
ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def big(text):
    rows = [""] * 5
    for ch in text:
        glyph = FONT.get(ch)
        if glyph:
            for i in range(5):
                rows[i] += glyph[i].replace("#", "██").replace(" ", "  ") + "  "
    return rows


def deg(value):
    """'63.7F' -> '64°'"""
    try:
        return f"{round(float(str(value).rstrip('FC')))}°"
    except (TypeError, ValueError):
        return "?"


def center(line, width):
    pad = max(0, (width - len(ANSI.sub("", line))) // 2)
    return " " * pad + line


class Dashboard:
    LOG_STYLE = {"you": ("cyan", "You"), "dean": ("green", "Dean"), "text-you": ("cyan", "You (text)"),
                 "text-dean": ("green", "Dean (text)"), "status": ("dim", "·"),
                 "warn": ("yellow", "!"), "err": ("red", "✗")}

    def __init__(self, toolbox):
        self.tb = toolbox
        self.lock = threading.Lock()
        self.log_lines = deque(maxlen=60)
        self.state = 'Say "hey Dean"'
        self.weather = None
        self.lights = []
        self.sensors = []
        threading.Thread(target=self._refresh, daemon=True, name="dash-data").start()
        threading.Thread(target=self._draw_loop, daemon=True, name="dash-draw").start()

    # ----- inputs -----

    def log(self, kind, text):
        with self.lock:
            self.log_lines.append((datetime.now(), kind, text))
        self.draw()

    def set_state(self, text):
        self.state = text
        self.draw()

    def _refresh(self):
        last_weather = 0
        while True:
            if time.time() - last_weather > 900:
                try:
                    self.weather = self.tb.get_weather(days=1)
                    last_weather = time.time()
                except Exception:
                    pass
            try:
                s = self.tb.climate_sensors()
                self.sensors = s if isinstance(s, list) else []
            except Exception:
                pass
            try:
                lights = []
                for light in self.tb.govee.lights:
                    s = self.tb.govee.status(light) if light.get("lan_ip") else {}
                    lights.append((light["name"], s))
                self.lights = lights
            except Exception:
                pass
            time.sleep(60)

    # ----- drawing -----

    def _draw_loop(self):
        sys.stdout.write("\033[?25l\033[2J")  # hide cursor, clear
        while True:
            self.draw()
            time.sleep(1)

    def draw(self):
        try:
            w, h = shutil.get_terminal_size((60, 40))
            screen = self._compose(w, h)
            with self.lock:
                sys.stdout.write("\033[H" + "\n".join(l + "\033[K" for l in screen) + "\033[J")
                sys.stdout.flush()
        except Exception:
            pass

    def _compose(self, w, h):
        now = datetime.now()
        out = [""]
        clock = now.strftime("%I:%M").lstrip("0")
        rows = big(clock) if w >= 44 else [C["bold"] + clock + C["off"]]
        ampm = now.strftime("%p")
        for i, r in enumerate(rows):
            tail = f" {C['dim']}{ampm}{C['off']}" if i == len(rows) - 1 else ""
            out.append(center(f"{C['bold']}{r}{C['off']}{tail}", w))
        out.append(center(f"{C['dim']}{now.strftime('%A, %B')} {now.day}{C['off']}", w))
        out.append("")

        for a in (self.tb.active_alerts or [])[:2]:
            color = "red" if a["level"] == "warning" else "yellow"
            out.append(f"  {C[color]}{C['bold']}⚠ {a['event']}{C['off']}")
        wx = self.weather
        if wx and "now" in wx:
            n, d = wx["now"], (wx.get("days") or [{}])[0]
            out.append(f"  {C['bold']}{deg(n.get('temp'))}{C['off']} {n.get('conditions', '')}  "
                       f"{C['dim']}·{C['off']}  high {deg(d.get('high'))}  low {deg(d.get('low'))}  "
                       f"{C['dim']}·{C['off']}  {d.get('chance_of_precipitation', '?')} rain")
        for s in self.sensors:
            out.append(f"  {C['bold']}{deg(s['temperature'].rstrip('°FC'))}{C['off']} "
                       f"{s['name']}  {C['dim']}·{C['off']}  {s['humidity']} humidity"
                       + (f"  {C['dim']}(heard {s['last_heard']}){C['off']}"
                          if s["last_heard"] != "just now" else ""))
        if self.lights:
            parts = []
            for name, s in self.lights:
                short = name.replace(" ceiling light", "").replace(" bulb", "").title()
                if s.get("on"):
                    parts.append(f"{C['yellow']}●{C['off']} {short} {s.get('brightness', '')}%")
                elif "on" in s:
                    parts.append(f"{C['dim']}○ {short}{C['off']}")
                else:  # no reply: usually the wall switch is off
                    parts.append(f"{C['dim']}⊘ {short} offline{C['off']}")
            if parts:
                out.append("  " + "   ".join(parts))
        try:
            nxt = self.tb.scheduler.upcoming(limit=3) if self.tb.scheduler else []
        except Exception:
            nxt = []
        for item in nxt:
            icon = {"alarm": "⏰", "timer": "⏲", "routine": "↻"}.get(item["kind"], "•")
            out.append(f"  {C['blue']}{icon}{C['off']} {item['what']}  {C['dim']}{item['when']}{C['off']}")
        try:
            people = self.tb.presence.summary()
            if isinstance(people, list) and people:
                bits = [f"{C['green']}●{C['off']} {p['name']}" if p["home"]
                        else f"{C['dim']}○ {p['name']}{C['off']}" for p in people]
                out.append("  ⌂ " + "   ".join(bits))
        except Exception:
            pass
        try:
            lists = self.tb.list_show()
            counts = [f"{n}: {len(i)}" for n, i in lists.items() if isinstance(i, list) and i]
            if counts:
                out.append(f"  {C['blue']}☰{C['off']} {C['dim']}{'  ·  '.join(counts)}{C['off']}")
        except Exception:
            pass
        out.append(C["dim"] + "─" * w + C["off"])

        # conversation fills the rest, newest at the bottom
        room = max(0, h - len(out) - 2)
        convo = []
        with self.lock:
            entries = list(self.log_lines)
        for ts, kind, text in entries:
            color, label = self.LOG_STYLE.get(kind, ("dim", "·"))
            prefix = f"{ts.strftime('%I:%M').lstrip('0'):>5} {label}  "
            for i, piece in enumerate(textwrap.wrap(text, max(10, w - len(prefix) - 1)) or [""]):
                lead = prefix if i == 0 else " " * len(prefix)
                bold = C["bold"] if kind in ("you", "dean", "text-you", "text-dean") and i == 0 else ""
                convo.append(f"{C['dim']}{lead[:6]}{C['off']}{C[color]}{bold}{lead[6:]}{C['off']}"
                             f"{C[color]}{piece}{C['off']}")
        convo = convo[-room:] if room else []
        out += [""] * (room - len(convo)) + convo
        out.append("")
        out.append(C["rev"] + f" {self.state} ".ljust(w)[:w] + C["off"])
        return out[:h]
