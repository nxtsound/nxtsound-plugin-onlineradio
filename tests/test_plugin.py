"""The radio plugin against the NxtSound plugin protocol and its own logic (radio-browser.info is simulated)."""
from tests.conftest import AUDIO, BIGFM, DOWN, HLS, ICY, ICYNONE, MAIN, NOCT, SECOND, SRF3, SRF3B, SWR3, icy_body
from tests.vendor import plugincheck, pluginproto


def test_speaks_the_plugin_protocol(client):
    """The same check the server provides for every plugin (copy in tests/vendor)."""
    assert plugincheck.check("http://plugin.test", "secret", "swr3", transport=client._transport) == []


def test_manifest_adds_the_radio_tab_and_no_song_search(client):
    m = pluginproto.validate_manifest(client.get("/manifest").json())
    assert m["id"] == "onlineradio" and m["name"] and m["description"] and m["version"]
    assert m["categories"] == [{"id": "radio", "label": {"en": "Live radio", "de": "Live-Radio"}}]
    assert m["search_kinds"] == [] and set(m["features"]) == {"search", "stream", "suggest", "nowplaying"}


def test_requires_token(anon):
    assert anon.get("/manifest").status_code == 401
    assert anon.get("/search", params={"q": "x", "kind": "radio"}, headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert anon.get(f"/stream/{SWR3}").status_code == 401


def test_search_maps_stations_to_tracks(client):
    items = client.get("/search", params={"q": "a", "kind": "radio"}).json()["items"]
    by_id = {i["id"]: i for i in items}
    assert by_id[SWR3] == {"id": SWR3, "title": "SWR3", "artist": "Germany", "artist_id": "", "album": "news, pop", "album_id": "", "duration": "",
                           "thumb": "https://swr3.example/logo.png", "live": True}
    assert by_id[BIGFM]["artist"] == "rap, hip hop" and by_id[BIGFM]["album"] == "" and by_id[BIGFM]["thumb"] == ""  # no country: tags; http image dropped
    assert HLS not in by_id  # HLS streams cannot be passed on


def test_search_does_not_filter_by_bitrate_and_ranks_by_popularity(client, world):
    ids = [i["id"] for i in client.get("/search", params={"q": "bigfm", "kind": "radio", "limit": 7}).json()["items"]]
    assert ids == [BIGFM]  # bigFM reports bitrate 0 and must still be found
    p = world.searches[-1]
    assert p["order"] == "votes" and p["reverse"] == "true" and p["limit"] == "7" and p["hidebroken"] == "true" and "bitrateMin" not in p


def test_only_the_radio_category_is_searched(client, world):
    for kind in ("songs", "artists", "albums"):
        assert client.get("/search", params={"q": "swr3", "kind": kind}).json() == {"items": []}
    assert world.searches == []  # nothing was asked from radio-browser


def test_suggestions_are_unique_station_names(client):
    assert client.get("/suggest", params={"q": "a"}).json() == {"suggestions": ["SWR3", "SRF 3", "Radio SRF 3", "bigFM Deutsch Rap", "Icy Station", "Silent Station", "Dead station", "Odd content type"]}  # most popular first


def test_radio_browser_down_is_a_clean_502(client, world):
    world.rb_down = True
    assert client.get("/search", params={"q": "swr3", "kind": "radio"}).status_code == 502
    assert client.get("/suggest", params={"q": "swr3"}).status_code == 502
    assert client.get(f"/stream/{SWR3}").status_code == 502


def test_stream_passes_the_audio_through(client):
    r = client.get(f"/stream/{SWR3}")
    assert r.status_code == 200 and r.content == AUDIO and r.headers["content-type"] == "audio/mpeg"  # the station's audio/* type wins (parameters dropped)


def test_stream_sets_an_audio_type_when_the_station_does_not(client):
    assert client.get(f"/stream/{BIGFM}").headers["content-type"] == "audio/aac"  # none sent: from the codec
    assert client.get(f"/stream/{NOCT}").headers["content-type"] == "audio/ogg"  # application/octet-stream is not audio/*: from the codec


def test_stream_errors(client):
    assert client.get("/stream/not-an-id").status_code == 404
    assert client.get("/stream/ffffffff-ffff-ffff-ffff-ffffffffffff").status_code == 404  # radio-browser does not know it
    r = client.get(f"/stream/{DOWN}")
    assert r.status_code == 502 and "503" in r.json()["detail"]  # the station is down: quickly an error instead of hanging


# ---------- Mirrors and settings ----------

import app as appmod  # noqa: E402


def put(client, **values):
    r = client.put("/settings", json={"values": {"mirror": MAIN, "fallback": SECOND, **values}})
    assert r.status_code == 200, r.text
    return r.json()


def test_settings_offer_every_mirror_from_the_list(client):
    m = pluginproto.validate_manifest(client.get("/manifest").json())
    by = {s["key"]: s for s in m["settings"]}
    hosts = [host for host, _ in appmod.MIRRORS]
    assert [c["value"] for c in by["mirror"]["choices"]] == hosts  # adding a line to MIRRORS is all it takes
    assert [c["value"] for c in by["fallback"]["choices"]] == ["none"] + hosts
    assert by["mirror"]["default"] == hosts[0] and by["fallback"]["default"] == (hosts[1] if len(hosts) > 1 else "none")


def test_the_main_mirror_is_asked_first(client, world):
    put(client, mirror=SECOND, fallback=MAIN)
    client.get("/search", params={"q": "bigfm", "kind": "radio"})  # a term without spelling variants: one request
    assert world.hosts == [SECOND]


def test_the_fallback_is_used_when_the_main_mirror_fails(client, world):
    put(client, mirror=MAIN, fallback=SECOND)
    world.down_hosts.add(MAIN)
    assert [i["id"] for i in client.get("/search", params={"q": "bigfm", "kind": "radio"}).json()["items"]] == [BIGFM]
    assert world.hosts == [MAIN, SECOND]
    world.hosts.clear(); world.down_hosts.clear(); world.error_hosts.add(MAIN)  # HTTP 503 counts as a failure too
    assert client.get(f"/stream/{SWR3}").status_code == 200
    assert world.hosts[:2] == [MAIN, SECOND]
    info = {i["label"]["en"]: i["value"] for i in client.get("/settings").json()["info"]}
    assert info["Mirror in use"] == SECOND and info["Last problem"] == f"{MAIN}: HTTP 503"


def test_without_a_fallback_a_failing_mirror_is_a_502(client, world):
    put(client, mirror=MAIN, fallback="none")
    world.down_hosts.add(MAIN)
    assert client.get("/search", params={"q": "bigfm", "kind": "radio"}).status_code == 502
    assert world.hosts == [MAIN]  # no second attempt
    world.hosts.clear()
    put(client, mirror=MAIN, fallback=MAIN)  # the same mirror twice is asked once
    client.get("/search", params={"q": "bigfm", "kind": "radio"})
    assert world.hosts == [MAIN]


def test_unknown_mirrors_are_ignored(client, world):
    put(client, mirror="evil.example", fallback="also.evil.example")
    client.get("/search", params={"q": "swr3", "kind": "radio"})
    assert set(world.hosts) <= {host for host, _ in appmod.MIRRORS}  # only mirrors from the list are ever contacted
    assert client.put("/settings", json={"nope": 1}).status_code == 400


# ---------- Adding a station ----------

def add(client, **values):
    return client.post("/actions/add_station", json={"values": values}).json()


def test_the_manifest_declares_the_add_station_form(client):
    m = pluginproto.validate_manifest(client.get("/manifest").json())
    a = m["actions"][0]
    assert a["id"] == "add_station" and [f["key"] for f in a["fields"]] == ["name", "url", "homepage", "favicon", "countrycode", "languagecodes", "tags"]
    assert {f["key"] for f in a["fields"] if f["required"]} == {"name", "url"} and all(f["type"] == "text" for f in a["fields"])


def test_a_station_is_posted_to_radio_browser(client, world):
    r = add(client, name="My Station", url="https://example.com/stream.mp3", countrycode="de", tags="pop,rock")
    assert r["ok"] is True and "550e8400-e29b-11d4-a716-446655440000" in r["message"]["en"] and "hinzugefügt" in r["message"]["de"]
    assert world.added == [{"name": "My+Station", "url": "https%3A%2F%2Fexample.com%2Fstream.mp3", "countrycode": "DE", "tags": "pop%2Crock"}]  # empty fields are left out


def test_a_refusal_from_radio_browser_is_passed_on(client, world):
    world.add_answer = {"ok": False, "message": "station with this url already exists"}
    r = add(client, name="Dup", url="https://example.com/s")
    assert r["ok"] is False and "already exists" in r["message"]["en"]


def test_bad_input_is_refused_before_anything_is_sent(client, world):
    for bad in ({"url": "ftp://x/y"}, {"url": "example.com/stream"}, {"homepage": "nope"}, {"favicon": "javascript:alert(1)"}, {"countrycode": "DEU"}, {"countrycode": "1a"}):
        r = add(client, **{"name": "N", "url": "https://example.com/s", **bad})
        assert r["ok"] is False and r["message"]["en"], bad
    assert world.added == []


def test_adding_uses_the_fallback_mirror_too(client, world):
    put(client, mirror=MAIN, fallback=SECOND)
    world.down_hosts.add(MAIN)
    assert add(client, name="N", url="https://example.com/s")["ok"] is True
    assert world.hosts == [MAIN, SECOND]


def test_actions_need_the_token(anon):
    assert anon.post("/actions/add_station", json={"values": {"name": "N", "url": "https://x/y"}}).status_code == 401
    assert anon.put("/settings", json={"values": {}}).status_code == 401


# ---------- Start buffer ----------

def chunks(n, size=8192):
    return (bytes([i % 251]) * size for i in range(n))


def test_the_start_buffer_is_collected_and_handed_over_as_one_block():
    out = list(appmod.with_preroll(chunks(6), want=20000, max_wait=5))
    assert len(out[0]) == 3 * 8192 and len(out) == 1 + 3  # three chunks reach 20000 bytes, the other three follow one by one
    assert b"".join(out) == b"".join(chunks(6))  # nothing lost, nothing reordered


def test_without_a_start_buffer_nothing_is_held_back():
    assert [len(c) for c in appmod.with_preroll(chunks(3), want=0, max_wait=5)] == [8192, 8192, 8192]


def test_a_short_stream_is_still_passed_on_completely():
    assert b"".join(appmod.with_preroll(chunks(2), want=10**6, max_wait=5)) == b"".join(chunks(2))
    assert list(appmod.with_preroll(iter(()), want=100, max_wait=5)) == []


def test_a_slow_source_does_not_hold_the_start_back_for_ever():
    ticks = iter(range(0, 100, 3))  # a fake clock: 3 s pass per chunk
    out = list(appmod.with_preroll(chunks(10), want=10**9, max_wait=5, clock=lambda: next(ticks)))
    assert len(out[0]) == 2 * 8192 and len(out) == 1 + 8  # gave up after the wait limit, the rest follows chunk by chunk


def test_the_buffer_is_sized_from_the_bitrate():
    assert appmod.preroll_bytes({"bitrate": 128}, 3) == 48000
    assert appmod.preroll_bytes({"bitrate": 96}, 10) == 120000
    assert appmod.preroll_bytes({"bitrate": 0}, 2) == 32000 and appmod.preroll_bytes({}, 2) == 32000  # unknown bitrate: 128 kbit/s
    assert appmod.preroll_bytes({"bitrate": 128}, 0) == 0


def test_the_start_buffer_setting_is_offered_and_validated(client):
    m = pluginproto.validate_manifest(client.get("/manifest").json())
    s = next(x for x in m["settings"] if x["key"] == "buffer_seconds")
    assert (s["min"], s["max"], s["default"]) == (0, 30, 3) and s["unit"]["en"] == "seconds"
    assert appmod.SETTINGS["buffer_seconds"] == 3
    put(client, buffer_seconds=10)
    assert appmod.SETTINGS["buffer_seconds"] == 10
    for bad in (31, -1, "5", True, None):
        put(client, buffer_seconds=bad)
    assert appmod.SETTINGS["buffer_seconds"] == 10  # invalid values change nothing


def test_the_stream_is_identical_with_and_without_a_start_buffer(client):
    put(client, buffer_seconds=0)
    plain = client.get(f"/stream/{SWR3}").content
    put(client, buffer_seconds=5)
    assert client.get(f"/stream/{SWR3}").content == plain == AUDIO


# ---------- Spellings of the search term ----------

def test_spellings_of_the_search_term():
    v = appmod.name_variants
    assert v("srf3") == ["srf3", "srf 3"] and v("SRF 3") == ["SRF 3", "SRF3"]  # a space between letters and digits, or none at all
    assert v("sunshine live") == ["sunshine live", "sunshinelive"] and v("swr3 rock") == ["swr3 rock", "swr 3 rock", "swr3rock"]
    assert v("bigfm") == ["bigfm"] and v("  srf   3 ") == ["srf 3", "srf3"]  # nothing to vary / extra spaces are cleaned up
    assert v("SrF3")[1] == "SrF 3"  # the case of what was typed is kept: radio-browser ignores it


def test_srf3_is_found_although_it_is_stored_as_srf_3(client, world):
    for q in ("srf3", "SRF3", "Srf 3", "srf 3"):
        ids = [i["id"] for i in client.get("/search", params={"q": q, "kind": "radio"}).json()["items"]]
        assert ids == [SRF3, SRF3B], q  # both spellings are asked, the answers merged, the most popular station first
    assert {p["name"] for p in world.searches[:2]} == {"srf3", "srf 3"}


def test_the_merged_answer_has_no_duplicates_and_respects_the_limit(client):
    ids = [i["id"] for i in client.get("/search", params={"q": "srf 3", "kind": "radio"}).json()["items"]]
    assert ids == [SRF3, SRF3B] and len(set(ids)) == 2  # "srf 3" and "srf3" both reach the same stations: each appears once
    assert len(client.get("/search", params={"q": "a", "kind": "radio", "limit": 2}).json()["items"]) == 2


def test_a_failing_variant_does_not_break_the_search(client, monkeypatch):
    real = appmod._search_one

    def flaky(name, limit, timeout):
        if name == "swr 3":
            raise appmod.HTTPException(502, "boom")
        return real(name, limit, timeout)

    monkeypatch.setattr(appmod, "_search_one", flaky)
    assert [i["id"] for i in client.get("/search", params={"q": "swr3", "kind": "radio"}).json()["items"]] == [SWR3]  # the variant failed, the term as typed answers


def test_the_term_as_typed_failing_fails_the_search(client, monkeypatch):
    def broken(name, limit, timeout):
        raise appmod.HTTPException(502, "boom")

    monkeypatch.setattr(appmod, "_search_one", broken)
    assert client.get("/search", params={"q": "srf3", "kind": "radio"}).status_code == 502


def test_suggestions_ignore_case(client):
    from tests import conftest
    extra = {"stationuuid": "cccccccc-0000-0000-0000-000000000000", "name": "swr3", "url_resolved": "https://stream.example/x", "country": "Germany", "tags": "",
             "favicon": "", "codec": "MP3", "hls": 0, "votes": 1}
    conftest.STATIONS[extra["stationuuid"]] = extra
    try:
        names = client.get("/suggest", params={"q": "swr3"}).json()["suggestions"]
    finally:
        del conftest.STATIONS[extra["stationuuid"]]
    assert [n.lower() for n in names].count("swr3") == 1  # "SWR3" and "swr3" are one suggestion


# ---------- What is playing right now ----------

def now(client, sid=ICY):
    return client.get(f"/nowplaying/{sid}")


def test_the_current_song_is_read_from_the_stream_metadata(client, world):
    assert now(client).json() == {"title": "Wonderwall", "artist": "Oasis"}
    assert world.icy_requests == 1


def test_title_without_artist_and_other_encodings(client, world):
    world.icy_meta = b"StreamTitle='Just a title';"
    assert now(client).json() == {"title": "Just a title", "artist": ""}
    appmod._now.clear()
    world.icy_meta = "StreamTitle='Motörhead - Ace of Spades';".encode("latin-1")  # ISO-8859-1, as many stations still send it
    assert now(client).json() == {"title": "Ace of Spades", "artist": "Motörhead"}
    appmod._now.clear()
    world.icy_meta = "StreamTitle='Beyoncé - Halo';".encode("utf-8")
    assert now(client).json() == {"title": "Halo", "artist": "Beyoncé"}


def test_no_usable_title_means_empty_texts(client, world):
    for meta in (b"", b"StreamTitle='';", b"StreamTitle='-';", b"StreamTitle='...';", b"StreamUrl='http://x';", b"StreamTitle='unknown';"):
        appmod._now.clear()
        world.icy_meta = meta
        assert now(client).json() == {"title": "", "artist": ""}, meta


def test_a_station_without_metadata_is_not_an_error(client):
    assert now(client, ICYNONE).json() == {"title": "", "artist": ""}
    assert now(client, DOWN).json() == {"title": "", "artist": ""}  # the station does not answer: no information, no error


def test_the_answer_is_reused_for_a_few_seconds(client, world):
    now(client)
    world.icy_meta = b"StreamTitle='Other - Song';"
    assert now(client).json()["title"] == "Wonderwall" and world.icy_requests == 1  # the second ask did not bother the station
    appmod._now.clear()
    assert now(client).json()["title"] == "Song"


def test_unknown_stations_and_ids(client):
    assert now(client, "ffffffff-ffff-ffff-ffff-ffffffffffff").status_code == 404
    assert now(client, "not-an-id").status_code == 404


def test_now_playing_needs_the_token(anon):
    assert anon.get(f"/nowplaying/{ICY}").status_code == 401


def test_parse_icy_edge_cases():
    p = appmod.parse_icy
    assert p(b"StreamTitle='A - B - C';") == {"title": "B - C", "artist": "A"}  # only the first " - " separates
    assert p(b"StreamTitle='It's a song';StreamUrl='';") == {"title": "It's a song", "artist": ""}  # an apostrophe inside the title
    assert p(b"") == {} and p(b"\0\0") == {}


def test_oversized_or_invalid_metaint_is_ignored(monkeypatch, client, world):
    import httpx
    def handler(req):
        return httpx.Response(200, content=icy_body(b"StreamTitle='X - Y';"), headers={"icy-metaint": "999999999"})
    transport = httpx.MockTransport(handler)
    class C(httpx.Client):
        def __init__(self, **kw):
            super().__init__(transport=transport, **kw)
    monkeypatch.setattr(appmod.httpx, "Client", C)
    assert appmod.read_icy("https://x.example/s") == {}
