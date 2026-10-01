"""Calendar events (from private iCal feeds) and news headlines (RSS) for Dean.

Calendars: put one or more private feed links in ~/.dean.env, comma-separated:
  DEAN_CALENDARS=https://calendar.google.com/calendar/ical/.../private-.../basic.ics,webcal://p01-caldav.icloud.com/published/2/...
  * Google Calendar: Settings > (your calendar) > Integrate calendar > "Secret address in iCal format"
  * iCloud: Calendar app > calendar info > Public Calendar > share link
  * Outlook: Settings > Calendar > Shared calendars > Publish a calendar > ICS link
These links work like passwords; keep them only in ~/.dean.env.
"""

import os
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import httpx
import icalendar
import recurring_ical_events

_cache = {}  # url -> (fetched_at, Calendar)
NEWS_TOP = "https://feeds.npr.org/1001/rss.xml"
NEWS_SEARCH = "https://news.google.com/rss/search"


def _calendar(url):
    url = url.strip().replace("webcal://", "https://", 1)
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    r = httpx.get(url, timeout=15, follow_redirects=True)
    r.raise_for_status()
    cal = icalendar.Calendar.from_ical(r.content)
    _cache[url] = (time.time(), cal)
    return cal


def _local(value):
    """iCal start/end (date or aware/naive datetime) -> (local naive datetime, all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo:
            value = value.astimezone().replace(tzinfo=None)
        return value, False
    return datetime.combine(value, datetime.min.time()), True


def events(start_day, days=1, urls=None):
    urls = urls if urls is not None else [u for u in os.environ.get("DEAN_CALENDARS", "").split(",") if u.strip()]
    if not urls:
        return {"error": "no calendars connected yet (DEAN_CALENDARS in ~/.dean.env)"}
    start = datetime.combine(start_day, datetime.min.time())
    end = start + timedelta(days=days)
    out, failed = [], 0
    for url in urls:
        try:
            cal = _calendar(url)
        except Exception:
            failed += 1
            continue
        name = str(cal.get("X-WR-CALNAME", "") or "")
        for ev in recurring_ical_events.of(cal).between(start, end):
            s, all_day = _local(ev.decoded("DTSTART"))
            e = _local(ev.decoded("DTEND"))[0] if ev.get("DTEND") else None
            item = {"title": str(ev.get("SUMMARY", "(no title)")),
                    "day": s.strftime("%A %b %d"),
                    "time": "all day" if all_day else s.strftime("%I:%M %p").lstrip("0"),
                    "_sort": s.isoformat()}
            if e and not all_day:
                item["until"] = e.strftime("%I:%M %p").lstrip("0")
            if ev.get("LOCATION"):
                item["location"] = str(ev.get("LOCATION"))
            if name:
                item["calendar"] = name
            out.append(item)
    out.sort(key=lambda i: i.pop("_sort"))
    result = {"events": out} if out else {"events": [], "note": "nothing on the calendar"}
    if failed:
        result["warning"] = f"{failed} calendar feed(s) couldn't be loaded"
    return result


def headlines(topic=None, count=5):
    url, params = (NEWS_SEARCH, {"q": topic, "hl": "en-US", "gl": "US", "ceid": "US:en"}) if topic \
        else (NEWS_TOP, None)
    r = httpx.get(url, params=params, timeout=10, follow_redirects=True)
    r.raise_for_status()
    items = ET.fromstring(r.content).iter("item")
    out = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        if topic and source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]  # Google News appends " - Source"
        if title:
            out.append({"headline": title, **({"source": source} if source else {})})
        if len(out) >= count:
            break
    return out


def parse_day(day):
    """'today', 'tomorrow', a weekday name, or YYYY-MM-DD -> date."""
    d = (day or "today").strip().lower()
    today = date.today()
    if d == "today":
        return today
    if d == "tomorrow":
        return today + timedelta(days=1)
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if d in names:
        ahead = (names.index(d) - today.weekday()) % 7
        return today + timedelta(days=ahead)
    return date.fromisoformat(d)


GOOD_MORNING = (
    "Greet them warmly by time of day. Then, briefly and in this order: today's weather "
    "(get_weather: now, high, low, rain chance); today's calendar events (calendar_events for "
    "today; skip this if no calendar is connected); today's reminders and alarms "
    "(list_scheduled, only items for today); and three top news headlines (news_headlines). "
    "Keep the whole briefing under 45 seconds when spoken, with no lists or filler."
)
GOOD_NIGHT = (
    "Tell them tomorrow's first calendar event and any alarm set for tomorrow morning "
    "(calendar_events for tomorrow, list_scheduled), the overnight low (get_weather), then "
    "say good night. Keep it to two or three sentences."
)
