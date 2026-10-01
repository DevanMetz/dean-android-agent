"""Tests run on any computer: modules that only work on the tablet (audio, Termux:API)
aren't imported, and files under the tablet's home directory are redirected to a
temporary folder."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def tablet_home(tmp_path, monkeypatch):
    import govee
    import roku
    import scheduler
    import tools

    monkeypatch.setattr(scheduler, "SCHEDULE_FILE", tmp_path / "schedule.json")
    monkeypatch.setattr(scheduler, "ROUTINES_FILE", tmp_path / "routines.json")
    monkeypatch.setattr(govee, "LIGHTS_FILE", tmp_path / "lights.json")
    monkeypatch.setattr(roku, "ROKU_FILE", tmp_path / "roku.json")
    monkeypatch.setattr(tools, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(tools, "PLACE_FILE", tmp_path / "place.json")
    monkeypatch.setattr(tools, "SENSORS_FILE", tmp_path / "sensors.json")
    return tmp_path
