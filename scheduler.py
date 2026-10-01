"""Reminders, alarms, timers and scheduled routines for Dean.

Items live in ~/assistant/schedule.json so they survive restarts:
  {"id": "a1b2", "kind": "reminder" | "alarm" | "timer" | "routine",
   "text": "take out the trash", "at": "2026-10-01T19:00:00",
   "repeat": null | "daily" | "weekdays" | "weekends" | "weekly",
   "routine": "good morning" (alarms/routines), "notify": <telegram chat id>, "speak": true}
A background thread calls on_fire(item) when an item comes due, then either
removes it or moves it to its next occurrence.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

SCHEDULE_FILE = Path("/data/data/com.termux/files/home/assistant/schedule.json")
ROUTINES_FILE = Path("/data/data/com.termux/files/home/assistant/routines.json")
REPEATS = ("daily", "weekdays", "weekends", "weekly")
LATE_GRACE = timedelta(minutes=30)  # still fire items missed while Dean was restarting


def next_occurrence(at, repeat):
    nxt = at + timedelta(days=7 if repeat == "weekly" else 1)
    if repeat == "weekdays":
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    elif repeat == "weekends":
        while nxt.weekday() < 5:
            nxt += timedelta(days=1)
    return nxt


def describe(item):
    at = datetime.fromisoformat(item["at"])
    today = datetime.now().date()
    day = ("today" if at.date() == today else "tomorrow" if at.date() == today + timedelta(days=1)
           else at.strftime("%A %b %d"))
    when = at.strftime("%I:%M %p").lstrip("0")
    rep = f", repeats {item['repeat']}" if item.get("repeat") else ""
    label = item.get("text") or item.get("routine") or item["kind"]
    return {"id": item["id"], "kind": item["kind"], "what": label, "when": f"{day} {when}{rep}",
            "in_minutes": max(0, round((at - datetime.now()).total_seconds() / 60))}


class Scheduler:
    def __init__(self, on_fire=None, run=True):
        """run=False: only add/list/cancel (e.g. from dean.py --ask); the live Dean
        process notices the file change and does the firing."""
        self.on_fire = on_fire
        self.lock = threading.Lock()
        self.items = json.loads(SCHEDULE_FILE.read_text()) if SCHEDULE_FILE.exists() else []
        if run:
            self._catch_up()
            threading.Thread(target=self._loop, daemon=True, name="scheduler").start()

    # ----- persistence -----

    def _save(self):
        tmp = SCHEDULE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=1))
        tmp.replace(SCHEDULE_FILE)
        self._mtime = SCHEDULE_FILE.stat().st_mtime

    def _reload_if_changed(self):
        """Pick up items added by another process (e.g. dean.py --ask)."""
        try:
            mtime = SCHEDULE_FILE.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != getattr(self, "_mtime", None):
            self.items = json.loads(SCHEDULE_FILE.read_text())
            self._mtime = mtime

    def _catch_up(self):
        """Drop one-off items missed long ago; roll repeating ones forward."""
        now = datetime.now()
        keep = []
        for it in self.items:
            at = datetime.fromisoformat(it["at"])
            if at < now - LATE_GRACE:
                if not it.get("repeat"):
                    continue
                while at < now:
                    at = next_occurrence(at, it["repeat"])
                it["at"] = at.isoformat(timespec="seconds")
            keep.append(it)
        self.items = keep
        self._save()

    # ----- API -----

    def add(self, kind, at, text="", repeat=None, routine=None, notify=None, speak=True):
        if repeat not in (None, "none") + REPEATS:
            raise ValueError(f"repeat must be one of {REPEATS}")
        if at < datetime.now() - timedelta(seconds=5):
            if repeat in REPEATS:
                while at < datetime.now():
                    at = next_occurrence(at, repeat)
            else:
                raise ValueError("that time has already passed")
        item = {"id": uuid.uuid4().hex[:4], "kind": kind, "text": text,
                "at": at.isoformat(timespec="seconds"),
                "repeat": None if repeat in (None, "none") else repeat,
                "routine": routine, "notify": notify, "speak": speak}
        with self.lock:
            self._reload_if_changed()
            self.items.append(item)
            self._save()
        return describe(item)

    def upcoming(self, limit=None):
        with self.lock:
            self._reload_if_changed()
            items = sorted(self.items, key=lambda i: i["at"])
        return [describe(i) for i in items[:limit]]

    def cancel(self, match):
        m = match.lower().strip()
        with self.lock:
            self._reload_if_changed()
            hits = [i for i in self.items if m in ("all", "everything") or m == i["id"]
                    or m in (i.get("text") or "").lower() or m in (i.get("routine") or "").lower()
                    or m == i["kind"]]
            self.items = [i for i in self.items if i not in hits]
            self._save()
        return [describe(i)["what"] for i in hits]

    # ----- firing -----

    def _loop(self):
        while True:
            time.sleep(1)
            now = datetime.now()
            with self.lock:
                self._reload_if_changed()
                due = [i for i in self.items if datetime.fromisoformat(i["at"]) <= now]
                for it in due:
                    if it.get("repeat"):
                        it["at"] = next_occurrence(datetime.fromisoformat(it["at"]),
                                                   it["repeat"]).isoformat(timespec="seconds")
                    else:
                        self.items.remove(it)
                if due:
                    self._save()
            for it in due:
                threading.Thread(target=self._fire, args=(it,), daemon=True).start()

    def _fire(self, item):
        try:
            self.on_fire(item)
        except Exception as e:  # a failing reminder must not kill the scheduler
            print(f"scheduler: {item.get('text')!r} failed: {e}")


# ----- routines: named lists of plain-English steps the LLM carries out -----

def routines():
    return json.loads(ROUTINES_FILE.read_text()) if ROUTINES_FILE.exists() else {}


def save_routine(name, steps):
    r = routines()
    r[name.lower().strip()] = steps
    ROUTINES_FILE.write_text(json.dumps(r, indent=1))


def delete_routine(name):
    r = routines()
    gone = r.pop(name.lower().strip(), None)
    ROUTINES_FILE.write_text(json.dumps(r, indent=1))
    return gone is not None
