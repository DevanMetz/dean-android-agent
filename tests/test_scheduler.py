import json
from datetime import datetime, timedelta

import pytest

import scheduler
from scheduler import Scheduler, next_occurrence


def test_next_occurrence_skips_to_the_right_days():
    fri = datetime(2026, 10, 2, 7, 0)  # a Friday
    assert next_occurrence(fri, "daily") == datetime(2026, 10, 3, 7, 0)
    assert next_occurrence(fri, "weekdays") == datetime(2026, 10, 5, 7, 0)  # Monday
    assert next_occurrence(fri, "weekends") == datetime(2026, 10, 3, 7, 0)  # Saturday
    sun = datetime(2026, 10, 4, 9, 0)
    assert next_occurrence(sun, "weekends") == datetime(2026, 10, 10, 9, 0)  # next Saturday
    assert next_occurrence(fri, "weekly") == datetime(2026, 10, 9, 7, 0)


def test_add_list_cancel_round_trip():
    s = Scheduler(run=False)
    s.add("reminder", datetime.now() + timedelta(hours=1), text="call the dentist")
    s.add("timer", datetime.now() + timedelta(minutes=5), text="pasta")
    upcoming = s.upcoming()
    assert [u["what"] for u in upcoming] == ["pasta", "call the dentist"]  # soonest first
    assert s.cancel("dentist") == ["call the dentist"]
    assert [u["what"] for u in s.upcoming()] == ["pasta"]
    assert s.cancel("all") == ["pasta"]
    assert s.upcoming() == []


def test_past_one_off_is_rejected_but_repeating_rolls_forward():
    s = Scheduler(run=False)
    with pytest.raises(ValueError):
        s.add("reminder", datetime.now() - timedelta(hours=1), text="too late")
    item = s.add("alarm", datetime.now() - timedelta(hours=1), repeat="daily")
    assert item["in_minutes"] > 0


def test_changes_from_another_process_are_picked_up():
    live = Scheduler(run=False)
    other = Scheduler(run=False)  # e.g. dean.py --ask
    other.add("reminder", datetime.now() + timedelta(hours=2), text="water plants")
    assert [u["what"] for u in live.upcoming()] == ["water plants"]


def test_catch_up_drops_stale_one_offs_and_rolls_repeats():
    old = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    scheduler.SCHEDULE_FILE.write_text(json.dumps([
        {"id": "a", "kind": "reminder", "text": "stale", "at": old, "repeat": None},
        {"id": "b", "kind": "alarm", "text": "wake", "at": old, "repeat": "daily"},
    ]))
    s = Scheduler(run=False)
    s._catch_up()
    items = s.upcoming()
    assert [i["what"] for i in items] == ["wake"]
    assert datetime.fromisoformat(s.items[0]["at"]) > datetime.now()


def test_routines_are_saved_by_lowercase_name():
    scheduler.save_routine("Movie Time", "dim the living room to 20%")
    assert scheduler.routines() == {"movie time": "dim the living room to 20%"}
    assert scheduler.delete_routine("movie time") is True
    assert scheduler.routines() == {}
