import asyncio
import hmac
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

# ---------- Settings from the server ----------
# PLUGIN_TOKEN: access token, every request from the server carries it.
TOKEN = os.getenv("PLUGIN_TOKEN", "")

HEADERS = {"User-Agent": "nxtsound-plugin-onlineradio/0.2"}

# ---------- Server mirrors ----------
# radio-browser.info runs several servers ("mirrors") with the same data. In the plugin's settings the admin picks a main mirror and a
# fallback that is used when the main one does not answer.
#
# TO ADD A MIRROR: add one line to this list: (host name, label shown in the settings). Nothing else has to change, the settings pick it up.
# Which mirrors exist at the moment: https://all.api.radio-browser.info/json/servers
MIRRORS = [
    ("de1.api.radio-browser.info", "Germany 1 (de1)"),
]

# plugin.json is the single source for who the plugin is (id, name, version, description) and which search categories it adds.
# The manifest below is built from it, so renaming the plugin means editing that one file.
SPEC = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))

# ---------- Settings (shown in the admin area under the gear button) ----------
SETTINGS_SCHEMA = [
    {"key": "mirror", "type": "choice", "label": {"en": "Server mirror", "de": "Server-Mirror"}, "default": MIRRORS[0][0],
     "help": {"en": "The radio-browser.info server the plugin asks first.", "de": "Der radio-browser.info-Server, den das Plugin zuerst fragt."},
     "choices": [{"value": host, "label": label} for host, label in MIRRORS]},
    {"key": "fallback", "type": "choice", "label": {"en": "Fallback mirror", "de": "Fallback-Mirror"}, "default": MIRRORS[1][0] if len(MIRRORS) > 1 else "none",
     "help": {"en": "Used when the main mirror does not answer.", "de": "Wird benutzt, wenn der Haupt-Mirror nicht antwortet."},
     "choices": [{"value": "none", "label": {"en": "None", "de": "Keiner"}}] + [{"value": host, "label": label} for host, label in MIRRORS]},
    {"key": "buffer_seconds", "type": "int", "label": {"en": "Start buffer", "de": "Startpuffer"}, "unit": {"en": "seconds", "de": "Sekunden"},
     "min": 0, "max": 30, "default": 3,
     "help": {"en": "Seconds of audio collected before playback starts. The player then starts with a head start that bridges short hiccups of the station. "
                    "A higher value means fewer dropouts but a later start (stations that send in real time need that many seconds). 0 = start at once.",
              "de": "Sekunden Audio, die vor dem Abspielen gesammelt werden. Der Player startet dann mit einem Vorsprung, der kurze Hänger des Senders überbrückt. "
                    "Ein höherer Wert bedeutet weniger Aussetzer, aber späteren Start (Sender, die in Echtzeit senden, brauchen so viele Sekunden). 0 = sofort starten."}},
]
SETTINGS = {e["key"]: e["default"] for e in SETTINGS_SCHEMA}
_FIELDS = {e["key"]: e for e in SETTINGS_SCHEMA}


def _valid(entry: dict, value) -> bool:
    if entry["type"] == "int":
        return isinstance(value, int) and not isinstance(value, bool) and entry["min"] <= value <= entry["max"]
    return value in [c["value"] for c in entry["choices"]]  # choice: only what the list offers (for the mirrors: only mirrors from MIRRORS)


def apply_settings(raw) -> None:
    """Takes over valid values; the server sends them at start (PLUGIN_SETTINGS) and after every change (PUT /settings)."""
    for key, value in (raw if isinstance(raw, dict) else {}).items():
        if key in _FIELDS and _valid(_FIELDS[key], value):
            SETTINGS[key] = value


try:
    apply_settings(json.loads(os.getenv("PLUGIN_SETTINGS", "{}")))
except ValueError:
    pass

STATE = {"mirror": "", "problem": ""}  # which mirror answered last, and the last mirror that failed (for the settings sheet)


def rb(method: str, path: str, timeout: float = 8, **kw) -> httpx.Response:
    """Asks radio-browser.info: the main mirror first, then the fallback if that one does not answer or fails (5xx). Other answers are final."""
    hosts = [SETTINGS["mirror"]] + ([SETTINGS["fallback"]] if SETTINGS["fallback"] not in ("none", SETTINGS["mirror"]) else [])
    for host in hosts:
        try:
            r = httpx.request(method, f"https://{host}/json{path}", headers=HEADERS, timeout=timeout, **kw)
        except httpx.HTTPError as e:
            STATE["problem"] = f"{host}: {e.__class__.__name__}"
            continue
        if r.status_code >= 500:
            STATE["problem"] = f"{host}: HTTP {r.status_code}"
            continue
        STATE["mirror"] = host
        return r
    raise HTTPException(502, "radio-browser.info does not respond")


UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
CODEC_TYPES = {"MP3": "audio/mpeg", "AAC": "audio/aac", "AAC+": "audio/aac", "OGG": "audio/ogg", "FLAC": "audio/flac"}

def station_to_track(s: dict) -> dict:
    """A radio-browser station in the shape of the protocol: the station is the 'song'."""
    thumb = s.get("favicon") or ""
    tags = ", ".join(t for t in (s.get("tags") or "").split(",")[:2] if t)
    return {"id": s["stationuuid"], "title": s["name"].strip(), "artist": s.get("country") or tags, "artist_id": "",
            "album": tags if s.get("country") else "", "album_id": "", "duration": "",
            "thumb": thumb if thumb.startswith("https://") else "",  # only https images are allowed
            "live": True} 


def preroll_bytes(station: dict, seconds: int) -> int:
    """How many bytes of the stream make `seconds` of audio (from the station's bitrate; many report 0, then 128 kbit/s is assumed)."""
    return int(seconds * (station.get("bitrate") or 128) * 125)


def with_preroll(chunks, want: int, max_wait: float, clock=time.monotonic):
    """Passes a stream on, but first collects `want` bytes (at most `max_wait` seconds) and hands them over as one block, so the player
    starts with a head start that bridges short hiccups of the source. A source that sends in real time delivers that block only after as many seconds."""
    chunks = iter(chunks)
    if want > 0:
        first, started = bytearray(), clock()
        for c in chunks:
            first += c
            if len(first) >= want or clock() - started >= max_wait:
                break
        if first:
            yield bytes(first)
    yield from chunks


def name_variants(q: str) -> list[str]:
    """The search term and the spellings radio-browser may know a station under: 'srf3' -> 'srf 3', 'sunshine live' -> 'sunshinelive'.
    Upper and lower case do not matter (radio-browser ignores it). At most three, the term as typed first."""
    q = " ".join(q.split())
    out = [q]
    for v in (re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", q), q.replace(" ", "")):  # a space between letters and digits / no spaces at all
        if v and v.lower() not in {x.lower() for x in out}:
            out.append(v)
    return out


def _search_one(name: str, limit: int, timeout: float) -> list[dict]:
    r = rb("GET", "/stations/search", timeout=timeout,
           params={"name": name, "limit": limit, "order": "votes", "reverse": "true", "hidebroken": "true"})
    try:
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "radio-browser.info does not respond")


# ---------- What is playing right now ----------
# Most stations send the current song inside the stream ("ICY metadata"): ask for it with the header Icy-MetaData: 1 and the station tells after
# every `icy-metaint` bytes of audio how long the next metadata block is. A short extra connection reads just that first block.
ICY_MAX_METAINT = 65536
NOW_TTL = 15  # seconds an answer is reused per station
_now: dict[str, tuple[float, dict]] = {}


def parse_icy(block: bytes) -> dict:
    """b"StreamTitle='Artist - Title';" -> {"title": "Title", "artist": "Artist"}; {} if there is no usable title."""
    try:
        text = block.rstrip(b"\0").decode("utf-8")
    except UnicodeDecodeError:
        text = block.rstrip(b"\0").decode("latin-1")  # many stations still send ISO-8859-1
    m = re.search(r"StreamTitle='(.*?)';", text, re.S) or re.search(r"StreamTitle='(.*)", text, re.S)
    title = m.group(1).strip() if m else ""
    if not title or title.strip("-. ") == "" or title.lower() in ("unknown", "n/a"):
        return {}
    artist = ""
    if " - " in title:
        artist, _, title = title.partition(" - ")
    return {"title": title.strip()[:200], "artist": artist.strip()[:200]}


def read_icy(url: str, timeout: float = 5) -> dict:
    """Connects to the station for a moment and reads its first metadata block."""
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True, headers={**HEADERS, "Icy-MetaData": "1"}) as client:
            with client.stream("GET", url) as resp:
                try:
                    metaint = int(resp.headers.get("icy-metaint", "0"))
                except ValueError:
                    return {}
                if resp.status_code != 200 or not 0 < metaint <= ICY_MAX_METAINT:
                    return {}
                buf, length = bytearray(), None
                for chunk in resp.iter_bytes(8192):
                    buf += chunk
                    if length is None and len(buf) > metaint:
                        length = buf[metaint] * 16  # the byte after the audio block says how many 16-byte units of metadata follow
                    if length is not None and len(buf) >= metaint + 1 + length:
                        return parse_icy(bytes(buf[metaint + 1:metaint + 1 + length])) if length else {}
    except httpx.HTTPError:
        pass  # an unreachable station or one that speaks "ICY 200 OK" instead of HTTP: no information, no error
    return {}


def find_stations(name: str, limit: int, timeout: float = 8) -> list[dict]:
    """Asks radio-browser for the term and its other spellings (in parallel), merges the answers without duplicates, most popular first.
    No filtering by bitrate (many big stations report 0). If only a variant fails, the rest still counts; if the term as typed fails, so does the search."""
    variants = name_variants(name)
    with ThreadPoolExecutor(max_workers=len(variants)) as ex:
        futures = [ex.submit(_search_one, v, limit, timeout) for v in variants]
        answers = [f.result() if i == 0 else _quietly(f) for i, f in enumerate(futures)]  # the first one raises if it failed
    seen, merged = set(), []
    for stations in answers:
        for s in stations:
            if s.get("stationuuid") and (s.get("name") or "").strip() and not s.get("hls") and s["stationuuid"] not in seen:  # HLS streams are not supported
                seen.add(s["stationuuid"])
                merged.append(s)
    merged.sort(key=lambda s: -(s.get("votes") or 0))  # stable: same votes keep the order they came in
    return merged[:limit]


def _quietly(future) -> list[dict]:
    try:
        return future.result()
    except HTTPException:
        return []


# ---------- Protection: only the server may ask ----------

def auth(request: Request) -> None:
    given = request.headers.get("authorization", "")
    if not TOKEN or not hmac.compare_digest(given.encode(), f"Bearer {TOKEN}".encode()):
        raise HTTPException(401, "Token missing or wrong")


app = FastAPI(title="Online-Radio", docs_url=None, redoc_url=None, openapi_url=None)
router = APIRouter(dependencies=[Depends(auth)])


# ---------- Protocol ----------

@router.get("/manifest")
def manifest():
    return {"id": SPEC["id"], "name": SPEC["name"], "version": SPEC["version"], "api_version": 1, "description": SPEC["description"],
            "features": ["search", "stream", "suggest", "nowplaying"], "categories": SPEC["categories"], "search_kinds": [],
            "settings": SETTINGS_SCHEMA, "actions": ACTIONS}


@router.put("/settings")
async def put_settings(request: Request):
    """The admin changed the settings: the complete set arrives as {"values": {...}} and takes effect at once."""
    try:
        apply_settings((await request.json())["values"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, 'Expected {"values": {...}}')
    return get_settings()


@router.get("/settings")
def get_settings():
    """Read-only facts shown below the settings: the mirror that answered last and the last problem."""
    return {"info": [
        {"label": {"en": "Mirror in use", "de": "Aktiver Mirror"}, "value": STATE["mirror"] or "-"},
        {"label": {"en": "Last problem", "de": "Letztes Problem"}, "value": STATE["problem"] or "-"},
    ]}


# ---------- Adding a station to radio-browser.info ----------

def _field(key: str, en: str, de: str, limit: int, required: bool = False, placeholder: str = "") -> dict:
    return {"key": key, "type": "text", "label": {"en": en, "de": de}, "max_length": limit, "required": required,
            **({"placeholder": placeholder} if placeholder else {})}


ACTIONS = [{
    "id": "add_station",
    "label": {"en": "Add a station to radio-browser.info", "de": "Sender zu radio-browser.info hinzufügen"},
    "help": {"en": "The station is added to the public database for everyone. Only add stations that really exist; it can take a while until it shows up in the search.",
             "de": "Der Sender kommt für alle in die öffentliche Datenbank. Nur Sender eintragen, die es wirklich gibt; es kann etwas dauern, bis er in der Suche erscheint."},
    "fields": [
        _field("name", "Name", "Name", 400, True),
        _field("url", "Stream address", "Stream-Adresse", 400, True, "https://example.com/stream.mp3"),
        _field("homepage", "Homepage", "Webseite", 400, False, "https://example.com/"),
        _field("favicon", "Logo address (jpg or png)", "Logo-Adresse (jpg oder png)", 400, False, "https://example.com/logo.png"),
        _field("countrycode", "Country code", "Ländercode", 2, False, "DE"),
        _field("languagecodes", "Language codes", "Sprachcodes", 40, False, "de,en"),
        _field("tags", "Tags (separated by commas)", "Tags (durch Komma getrennt)", 200, False, "pop,rock"),
    ],
}]


def _no(en: str, de: str) -> dict:
    return {"ok": False, "message": {"en": en, "de": de}}


@router.post("/actions/add_station")
async def add_station(request: Request):
    """Adds a station to radio-browser.info (POST /json/add). The server has already checked that name and url are filled in and not too long."""
    v = (await request.json()).get("values") or {}
    for key in ("url", "homepage", "favicon"):
        if v.get(key) and not re.match(r"^https?://\S+$", v[key]):
            return _no(f"'{key}' must be a full address starting with http:// or https://.", f"'{key}' muss eine vollständige Adresse mit http:// oder https:// sein.")
    code = (v.get("countrycode") or "").upper()
    if code and not re.match(r"^[A-Z]{2}$", code):
        return _no("The country code has two letters, e.g. DE.", "Der Ländercode hat zwei Buchstaben, z. B. DE.")
    form = {k: v[k] for k in ("name", "url", "homepage", "favicon", "languagecodes", "tags") if v.get(k)}
    if code:
        form["countrycode"] = code
    r = await asyncio.to_thread(rb, "POST", "/add", data=form)
    try:
        result = r.json()
    except ValueError:
        return _no(f"radio-browser.info answered with HTTP {r.status_code}.", f"radio-browser.info hat mit HTTP {r.status_code} geantwortet.")
    if result.get("ok") is True:
        return {"ok": True, "message": {"en": f"Station added (ID {result.get('uuid', '?')}).", "de": f"Sender hinzugefügt (ID {result.get('uuid', '?')})."}}
    reason = str(result.get("message") or "unknown reason")[:200]
    return _no(f"Not added: {reason}", f"Nicht hinzugefügt: {reason}")

@router.get("/search")
def search(q: str = Query(min_length=1), kind: str = "songs", limit: int = Query(25, ge=1, le=50)):
    if kind != "radio":
        return {"items": []}
    return {"items": [station_to_track(s) for s in find_stations(q, limit)]}


@router.get("/suggest")
def suggest(q: str = Query(min_length=1)):
    names, seen = [], set()
    for s in find_stations(q, 20, timeout=2):
        name = s["name"].strip()
        if name.lower() not in seen:  # "SWR3" and "Swr3" are the same suggestion
            seen.add(name.lower())
            names.append(name)
    return {"suggestions": names[:8]}


@router.get("/nowplaying/{sid}")
def nowplaying(sid: str):
    """The song the station plays right now ({"title", "artist"}, empty texts if the station does not tell). Cached for a few seconds."""
    if not UUID_RE.match(sid):
        raise HTTPException(404, "unknown ID")
    hit = _now.get(sid)
    if hit and time.monotonic() - hit[0] < NOW_TTL:
        return hit[1]
    try:
        found = rb("GET", f"/stations/byuuid/{sid}").json()
    except ValueError:
        raise HTTPException(502, "radio-browser.info does not respond")
    if not found:
        raise HTTPException(404, "unknown station")
    data = read_icy(found[0].get("url_resolved") or found[0]["url"])
    out = {"title": data.get("title", ""), "artist": data.get("artist", "")}
    if len(_now) > 300:
        _now.clear()
    _now[sid] = (time.monotonic(), out)
    return out


@router.get("/stream/{sid}")
def stream(sid: str):
    """Looks the station up, opens its stream and passes the bytes on. A live stream never ends and cannot seek (so: always 200, no Range)."""
    if not UUID_RE.match(sid):
        raise HTTPException(404, "unknown ID")
    try:
        found = rb("GET", f"/stations/byuuid/{sid}").json()
    except ValueError:
        raise HTTPException(502, "radio-browser.info does not respond")
    if not found:
        raise HTTPException(404, "unknown station")
    station = found[0]
    client = httpx.Client(timeout=httpx.Timeout(8, read=30), follow_redirects=True, headers=HEADERS)
    try:
        resp = client.send(client.build_request("GET", station.get("url_resolved") or station["url"]), stream=True)
    except httpx.HTTPError:
        client.close()
        raise HTTPException(502, "The station does not respond")
    if resp.status_code != 200:
        resp.close()
        client.close()
        raise HTTPException(502, f"The station answered with HTTP {resp.status_code}")
    sent = resp.headers.get("content-type", "").split(";")[0].strip()
    media_type = sent if sent.startswith("audio/") else CODEC_TYPES.get((station.get("codec") or "").upper(), "audio/mpeg")  # the server insists on audio/*

    want = preroll_bytes(station, SETTINGS["buffer_seconds"])

    def body():
        try:
            yield from with_preroll(resp.iter_bytes(8192), want, SETTINGS["buffer_seconds"] + 2)
        finally:
            resp.close()
            client.close()

    return StreamingResponse(body(), media_type=media_type)


app.include_router(router)
