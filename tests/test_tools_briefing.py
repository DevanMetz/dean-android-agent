import json
from datetime import date, datetime, timedelta

import icalendar
import pytest

import briefing
import tools
from dashboard import big, deg


def test_every_tool_schema_is_well_formed_and_implemented():
    names = set()
    for spec in tools.TOOLS:
        fn = spec["function"]
        assert fn["name"] not in names, f"duplicate tool {fn['name']}"
        names.add(fn["name"])
        params = fn["parameters"]
        assert params["type"] == "object"
        for req in params["required"]:
            assert req in params["properties"], f"{fn['name']}: {req} not a property"
        assert callable(getattr(tools.Toolbox, fn["name"], None)), f"{fn['name']} not implemented"
    json.dumps(tools.TOOLS)  # must be sendable as-is


def test_when_accepts_iso_times_and_minutes():
    t = tools.Toolbox._when("2026-10-01T19:00")
    assert t == datetime(2026, 10, 1, 19, 0)
    soon = tools.Toolbox._when(in_minutes=10)
    assert timedelta(minutes=9) < soon - datetime.now() <= timedelta(minutes=10)
    with pytest.raises(ValueError):
        tools.Toolbox._when()


def test_reminders_set_by_text_go_back_to_that_chat():
    assert tools.Toolbox._where({"channel": "telegram", "chat": 42}, None) == {"notify": 42, "speak": False}
    assert tools.Toolbox._where({"channel": "voice"}, None) == {"notify": None, "speak": True}
    assert tools.Toolbox._where({"channel": "telegram", "chat": 42}, True)["speak"] is True


def test_parse_day():
    today = date.today()
    assert briefing.parse_day("today") == today
    assert briefing.parse_day("tomorrow") == today + timedelta(days=1)
    assert briefing.parse_day("2026-12-24") == date(2026, 12, 24)
    assert briefing.parse_day("monday").weekday() == 0


def test_calendar_expands_recurring_events(monkeypatch):
    cal = icalendar.Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Home\r\n"
        "BEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Trash night\r\nDTSTART:20260105T190000\r\n"
        "DTEND:20260105T193000\r\nRRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:2\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261005\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n")
    monkeypatch.setattr(briefing, "_calendar", lambda url: cal)
    out = briefing.events(date(2026, 10, 5), 1, urls=["https://example.invalid/cal.ics"])
    assert [(e["title"], e["time"]) for e in out["events"]] == [("Holiday", "all day"),
                                                                 ("Trash night", "7:00 PM")]
    assert out["events"][1]["until"] == "7:30 PM"
    assert briefing.events(date(2026, 10, 6), 1, urls=["x"])["events"] == []


def test_dashboard_helpers():
    assert deg("63.7F") == "64°"
    assert deg(None) == "?"
    rows = big("12:30")
    assert len(rows) == 5 and all("█" in r for r in rows)
