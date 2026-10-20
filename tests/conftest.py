"""Test setup: radio-browser.info and the radio stations are replaced by stand-ins (no network)."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ["PLUGIN_TOKEN"] = "secret"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402

AUDIO = bytes(range(256)) * 16  # 4096 bytes of "radio"
MAIN = appmod.MIRRORS[0][0]  # the first mirror of the plugin's list (the tests do not depend on what else is in it)
SECOND = "second.api.radio-browser.info"  # a mirror that exists only in the tests, as the fallback
SWR3 = "240d28b9-7858-48d2-a816-9cf8e1875fe8"
BIGFM = "11111111-2222-3333-4444-555555555555"
HLS = "99999999-8888-7777-6666-555555555555"
DOWN = "00000000-0000-0000-0000-000000000000"
NOCT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ICY = "dddddddd-1111-2222-3333-444444444444"
ICYNONE = "dddddddd-5555-6666-7777-888888888888"
SRF3 = "bbbbbbbb-1111-2222-3333-444444444444"
SRF3B = "bbbbbbbb-5555-6666-7777-888888888888"

STATIONS = {
    SWR3: {"stationuuid": SWR3, "name": " SWR3 ", "url_resolved": "https://stream.example/swr3", "country": "Germany", "tags": "news,pop,rock",
           "favicon": "https://swr3.example/logo.png", "codec": "MP3", "bitrate": 128, "hls": 0, "votes": 19505},
    BIGFM: {"stationuuid": BIGFM, "name": "bigFM Deutsch Rap", "url_resolved": "https://stream.example/bigfm", "country": "", "tags": "rap,hip hop,deutsch",
            "favicon": "http://insecure.example/logo.png", "codec": "AAC", "bitrate": 0, "hls": 0},
    HLS: {"stationuuid": HLS, "name": "Only HLS", "url_resolved": "https://stream.example/live.m3u8", "country": "Germany", "tags": "", "favicon": "",
          "codec": "AAC", "bitrate": 0, "hls": 1},
    SRF3: {"stationuuid": SRF3, "name": "SRF 3", "url_resolved": "https://stream.example/srf3", "country": "Switzerland", "tags": "pop", "favicon": "", "codec": "MP3",
           "bitrate": 128, "hls": 0, "votes": 5629},
    SRF3B: {"stationuuid": SRF3B, "name": "Radio SRF 3", "url_resolved": "https://stream.example/srf3b", "country": "Switzerland", "tags": "", "favicon": "", "codec": "AAC",
            "bitrate": 96, "hls": 0, "votes": 796},
    ICY: {"stationuuid": ICY, "name": "Icy Station", "url_resolved": "https://stream.example/icy", "country": "Germany", "tags": "", "favicon": "", "codec": "MP3", "hls": 0},
    ICYNONE: {"stationuuid": ICYNONE, "name": "Silent Station", "url_resolved": "https://stream.example/silent", "country": "Germany", "tags": "", "favicon": "", "codec": "MP3", "hls": 0},
    DOWN: {"stationuuid": DOWN, "name": "Dead station", "url_resolved": "https://stream.example/dead", "country": "Germany", "tags": "", "favicon": "", "codec": "MP3", "hls": 0},
    NOCT: {"stationuuid": NOCT, "name": "Odd content type", "url_resolved": "https://stream.example/odd", "country": "Germany", "tags": "", "favicon": "", "codec": "OGG", "hls": 0},
}


def icy_body(meta: bytes, metaint: int = 16) -> bytes:
    """Audio, the length byte (in 16-byte units), the metadata padded with zeros, more audio: how stations send their metadata."""
    pad = (-len(meta)) % 16
    return b"A" * metaint + bytes([(len(meta) + pad) // 16]) + meta + b"\0" * pad + b"B" * 64


class World:
    """The outside world: records the requests to radio-browser and plays the stations."""

    def __init__(self):
        self.searches: list[dict] = []  # parameters of every search sent to radio-browser
        self.hosts: list[str] = []  # which radio-browser mirror was asked, in order
        self.added: list[dict] = []  # stations posted to /json/add
        self.add_answer = {"ok": True, "message": "station was added", "uuid": "550e8400-e29b-11d4-a716-446655440000"}
        self.icy_meta = b"StreamTitle='Oasis - Wonderwall';StreamUrl='';"  # what the ICY station sends right now
        self.icy_requests = 0
        self.rb_down = False
        self.down_hosts: set[str] = set()  # mirrors that do not answer
        self.error_hosts: set[str] = set()  # mirrors that answer with HTTP 503

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = req.url
        if url.host.endswith("radio-browser.info"):
            self.hosts.append(url.host)
            if self.rb_down or url.host in self.down_hosts:
                raise httpx.ConnectError("radio-browser is down")
            if url.host in self.error_hosts:
                return httpx.Response(503)
            assert req.headers.get("user-agent", "").startswith("nxtsound-plugin-onlineradio"), "radio-browser asks for its own User-Agent"
            if url.path == "/json/add":
                self.added.append({k: v for k, v in (kv.split("=", 1) for kv in req.content.decode().split("&"))})
                return httpx.Response(200, json=self.add_answer)
            if url.path == "/json/stations/search":
                params = dict(url.params)
                self.searches.append(params)
                term = params["name"].lower()
                hits = [s for s in STATIONS.values() if term in s["name"].lower() or term == "a"]
                return httpx.Response(200, json=hits[:int(params["limit"])])
            if url.path.startswith("/json/stations/byuuid/"):
                s = STATIONS.get(url.path.rsplit("/", 1)[1])
                return httpx.Response(200, json=[s] if s else [])
        if url.host == "stream.example":
            if url.path == "/icy":
                self.icy_requests += 1
                assert req.headers.get("icy-metadata") == "1", "the station is asked for its metadata"
                return httpx.Response(200, content=icy_body(self.icy_meta), headers={"icy-metaint": "16", "content-type": "audio/mpeg"})
            if url.path == "/silent":  # a station without metadata
                return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})
            if url.path == "/dead":
                return httpx.Response(503)
            ctype = {"/odd": "application/octet-stream", "/swr3": "audio/mpeg; charset=x"}.get(url.path)
            return httpx.Response(200, content=AUDIO, headers={"content-type": ctype} if ctype else {})
        return httpx.Response(404)


@pytest.fixture()
def world(monkeypatch):
    w = World()
    transport = httpx.MockTransport(w.handler)
    monkeypatch.setattr(appmod, "SETTINGS", {e["key"]: e["default"] for e in appmod.SETTINGS_SCHEMA})  # the plugin's settings are global: every test starts with the defaults
    monkeypatch.setattr(appmod, "STATE", {"mirror": "", "problem": ""})
    appmod._now.clear()
    fields = {k: {**e, **({"choices": e["choices"] + [{"value": SECOND, "label": "Second"}]} if e["type"] == "choice" else {})} for k, e in appmod._FIELDS.items()}
    monkeypatch.setattr(appmod, "_FIELDS", fields)  # the tests know one more mirror than the real list, to test the fallback

    class Client(httpx.Client):
        def __init__(self, **kw):
            super().__init__(transport=transport, **kw)

    shim = SimpleNamespace(request=lambda method, url, **kw: httpx.Client(transport=transport).request(method, url, **kw), Client=Client,
                           HTTPError=httpx.HTTPError, Timeout=httpx.Timeout)
    monkeypatch.setattr(appmod, "httpx", shim)
    return w


@pytest.fixture()
def client(world):
    with TestClient(appmod.app, headers={"Authorization": "Bearer secret"}) as c:
        yield c


@pytest.fixture()
def anon(world):
    with TestClient(appmod.app) as c:
        yield c
