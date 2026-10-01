"""Who's home: notices when household phones join or leave the home Wi-Fi.

People and their phones' addresses live in ~/assistant/people.json:
  [{"name": "Dev", "devices": [{"label": "Pixel 10", "ip": "192.168.1.94"},
                                {"label": "iPhone 17", "ip": "192.168.1.139"}]}]
Give the phones fixed addresses in the router (DHCP reservation) so they don't move.

A phone counts as present if it answers a TCP probe at all: an iPhone listens on
62078, and anything else on the network answers a closed port with a refusal, which
still proves it's there. Sleeping phones miss probes now and then, so someone is
only "away" after AWAY_AFTER seconds without an answer.
"""

import json
import socket
import threading
import time
from pathlib import Path

PEOPLE_FILE = Path("/data/data/com.termux/files/home/assistant/people.json")
PENDING_FILE = Path("/data/data/com.termux/files/home/assistant/arrival_reminders.json")
AWAY_AFTER = 15 * 60
CHECK_EVERY = 60


def probe(ip, timeout=1.5):
    for port in (62078, 7):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            return True
        except ConnectionRefusedError:
            return True  # a refusal comes from the phone itself
        except OSError:
            continue  # timed out or unreachable
        finally:
            s.close()
    return False


class Presence:
    def __init__(self, on_arrive=None, on_leave=None, prober=probe, clock=time.time):
        self.on_arrive = on_arrive or (lambda name: None)
        self.on_leave = on_leave or (lambda name: None)
        self.probe, self.clock = prober, clock
        self.lock = threading.Lock()
        self.state = {}  # name -> {"home": bool, "since": t, "last_seen": t}

    @staticmethod
    def people():
        return json.loads(PEOPLE_FILE.read_text()) if PEOPLE_FILE.exists() else []

    def check(self):
        """Probe everyone once and fire arrive/leave callbacks on changes."""
        now = self.clock()
        events = []
        for person in self.people():
            name = person["name"]
            seen = any(self.probe(d["ip"]) for d in person.get("devices", []))
            with self.lock:
                st = self.state.get(name)
                if st is None:  # first look: no event, just learn the state
                    st = self.state[name] = {"home": seen, "since": now,
                                             "last_seen": now if seen else 0}
                    continue
                if seen:
                    st["last_seen"] = now
                    if not st["home"]:
                        st.update(home=True, since=now)
                        events.append(("arrive", name))
                elif st["home"] and now - st["last_seen"] > AWAY_AFTER:
                    st.update(home=False, since=st["last_seen"])
                    events.append(("leave", name))
        for kind, name in events:
            (self.on_arrive if kind == "arrive" else self.on_leave)(name)
        return events

    def run_forever(self):
        while True:
            try:
                self.check()
            except Exception as e:
                print(f"presence check failed: {e}")
            time.sleep(CHECK_EVERY)

    def summary(self):
        if not self.people():
            return {"error": "no household phones set up yet (people.json)"}
        with self.lock:
            out = []
            for name, st in self.state.items():
                mins = int((self.clock() - st["since"]) / 60)
                out.append({"name": name, "home": st["home"],
                            "for": f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"})
            return out or {"note": "still checking"}

    # ----- "remind me when I get home" -----

    @staticmethod
    def pending():
        return json.loads(PENDING_FILE.read_text()) if PENDING_FILE.exists() else []

    def add_arrival_reminder(self, name, text, notify=None):
        items = self.pending()
        items.append({"name": name, "text": text, "notify": notify})
        PENDING_FILE.write_text(json.dumps(items, indent=1))

    def take_arrival_reminders(self, name):
        items = self.pending()
        mine = [i for i in items if i["name"].lower() == name.lower()]
        PENDING_FILE.write_text(json.dumps([i for i in items if i not in mine], indent=1))
        return mine
