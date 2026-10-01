import json

import httpx
import pytest

import govee
import roku
from govee import Govee, parse_color


def test_parse_color_names_and_hex():
    assert parse_color("Blue") == (0, 0, 255)
    assert parse_color("#ff8800") == (255, 136, 0)
    assert parse_color("00ff00") == (0, 255, 0)
    with pytest.raises(ValueError):
        parse_color("blurple")


def lights_file(entries):
    govee.LIGHTS_FILE.write_text(json.dumps(entries))
    return Govee()


def test_find_by_name_alias_room_word_and_all():
    g = lights_file([
        {"name": "bedroom ceiling light", "aliases": ["my room"], "lan_ip": "10.0.0.2"},
        {"name": "living room bulb", "aliases": ["living room"], "lan_ip": "10.0.0.3"},
        {"name": "bathroom bulb", "aliases": ["bathroom"], "lan_ip": "10.0.0.4"},
    ])
    names = lambda hits: [h["name"] for h in hits]  # noqa: E731
    assert names(g.find("my room")) == ["bedroom ceiling light"]
    assert names(g.find("the bathroom light")) == ["bathroom bulb"]
    assert len(g.find("all")) == 3
    assert g.find("garage") == []


def test_single_light_matches_any_request():
    g = lights_file([{"name": "bedroom ceiling light", "lan_ip": "10.0.0.2"}])
    assert [h["name"] for h in g.find("bad room lie")] == ["bedroom ceiling light"]


def fake_tv(handler):
    tv = roku.Roku(ip="10.0.0.9")
    tv.http = httpx.Client(transport=httpx.MockTransport(handler))
    return tv


def test_roku_launch_matches_app_names():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path == "/query/apps":
            return httpx.Response(200, text='<apps><app id="12" type="appl">Netflix</app>'
                                            '<app id="837" type="appl">YouTube</app></apps>')
        return httpx.Response(200)

    tv = fake_tv(handler)
    assert tv.launch("youtube") == "YouTube"
    assert ("POST", "/launch/837") in calls


def test_roku_reports_locked_network_control():
    tv = fake_tv(lambda req: httpx.Response(403))
    with pytest.raises(roku.RokuError, match="Control by mobile apps"):
        tv.press("Home")


def test_roku_status_parses_power_and_app():
    def handler(req):
        if req.url.path == "/query/device-info":
            return httpx.Response(200, text="<device-info><user-device-name>Den TV</user-device-name>"
                                            "<power-mode>PowerOn</power-mode></device-info>")
        if req.url.path == "/query/active-app":
            return httpx.Response(200, text='<active-app><app id="12">Netflix</app></active-app>')
        return httpx.Response(200, text='<player state="play"/>')

    assert fake_tv(handler).status() == {"name": "Den TV", "power": "on", "app": "Netflix",
                                         "playback": "play"}
