"""Active weather alerts from the US National Weather Service (api.weather.gov).

Free, no key; the NWS asks for a User-Agent that identifies the app.
"""

import httpx

NWS = "https://api.weather.gov/alerts/active"
HEADERS = {"User-Agent": "dean-wall-assistant (github.com/DevanMetz/dean-android-agent)",
           "Accept": "application/geo+json"}


def level(alert):
    """'warning' (announce at home), 'watch' (text + dashboard) or 'advisory' (dashboard)."""
    event = alert["event"].lower()
    if event.endswith("warning") or alert.get("severity") in ("Extreme", "Severe"):
        return "warning"
    if event.endswith("watch"):
        return "watch"
    return "advisory"


def active(lat, lon):
    r = httpx.get(NWS, params={"point": f"{lat:.4f},{lon:.4f}"}, headers=HEADERS, timeout=15)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        a = {"id": p.get("id"), "event": p.get("event", "Weather alert"),
             "severity": p.get("severity"), "headline": p.get("headline") or "",
             "ends": p.get("ends") or p.get("expires"),
             "instruction": (p.get("instruction") or "").strip()[:400],
             "description": (p.get("description") or "").strip()[:600]}
        a["level"] = level(a)
        out.append(a)
    order = {"warning": 0, "watch": 1, "advisory": 2}
    return sorted(out, key=lambda a: order[a["level"]])
