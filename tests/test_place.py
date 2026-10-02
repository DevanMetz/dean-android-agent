import json
import time

import tools


def test_stale_location_never_blocks(monkeypatch):
    """A day-old location is returned at once; the slow refresh happens in the background."""
    tools.PLACE_FILE.write_text(json.dumps(
        {"lat": 43.19, "lon": -87.96, "city": "Brown Deer", "tz": "America/Chicago",
         "at": time.time() - 3 * 86400}))
    calls = []

    def slow_fix(*args, **kwargs):
        calls.append(args)
        time.sleep(2)
        raise RuntimeError("timed out")

    monkeypatch.setattr(tools, "termux", slow_fix)
    tb = tools.Toolbox.__new__(tools.Toolbox)
    t0 = time.time()
    for _ in range(3):
        assert tb.place()["city"] == "Brown Deer"
    assert time.time() - t0 < 0.5
    time.sleep(0.1)
    assert len(calls) == 1  # one background attempt, not one per request
