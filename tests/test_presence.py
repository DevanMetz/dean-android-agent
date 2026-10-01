import json

import pytest

import presence
from presence import AWAY_AFTER, Presence


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, "PEOPLE_FILE", tmp_path / "people.json")
    monkeypatch.setattr(presence, "PENDING_FILE", tmp_path / "arrivals.json")
    presence.PEOPLE_FILE.write_text(json.dumps(
        [{"name": "Dev", "devices": [{"label": "Pixel", "ip": "10.0.0.5"}]}]))


def make(online, now):
    events = []
    p = Presence(on_arrive=lambda n: events.append(("arrive", n)),
                 on_leave=lambda n: events.append(("leave", n)),
                 prober=lambda ip: online[0], clock=lambda: now[0])
    return p, events


def test_arrive_immediately_but_leave_only_after_grace_period():
    online, now = [False], [0.0]
    p, events = make(online, now)
    p.check()                       # first look: learns "away", no event
    online[0], now[0] = True, 60
    p.check()
    assert events == [("arrive", "Dev")]
    online[0], now[0] = False, 120  # phone dozing: missed probe
    p.check()
    assert events == [("arrive", "Dev")]
    now[0] = 60 + AWAY_AFTER + 1
    p.check()
    assert events[-1] == ("leave", "Dev")
    assert p.summary()[0]["home"] is False


def test_arrival_reminders_are_delivered_once():
    p, _ = make([True], [0.0])
    p.add_arrival_reminder("Dev", "take the chicken out")
    p.add_arrival_reminder("Katie", "water the plants")
    assert [r["text"] for r in p.take_arrival_reminders("dev")] == ["take the chicken out"]
    assert p.take_arrival_reminders("Dev") == []
    assert [r["name"] for r in p.pending()] == ["Katie"]
