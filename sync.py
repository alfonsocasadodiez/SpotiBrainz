import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
TOKEN_FILE = DATA_DIR / "spotify_token.enc"
LEGACY_TOKEN_FILE = DATA_DIR / "spotify_token.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "last_sync.log"
# Kept outside DATA_DIR (its own Docker volume) so a copy of the data folder alone can't decrypt anything
KEY_FILE = Path(os.environ.get("KEY_FILE", "/keys/secret.key"))
SECRET_KEYS = ("SPOTIFY_CLIENT_SECRET", "LISTENBRAINZ_TOKEN")
ENC_PREFIX = "enc:v1:"

LB_API = os.environ.get("LISTENBRAINZ_API_URL", "https://api.listenbrainz.org").rstrip("/")
SPOTIFY_ACCOUNTS = "https://accounts.spotify.com"
SPOTIFY_API = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private user-read-private"
USER_AGENT = "SpotiBrainz/1.0 (https://github.com/alfonsocasadodiez/spotibrainz)"
JSPF_PLAYLIST_EXT = "https://musicbrainz.org/doc/jspf#playlist"
JSPF_TRACK_EXT = "https://musicbrainz.org/doc/jspf#track"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
SPOTIFY_LINK_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?track/([A-Za-z0-9]{22})")

DEFAULT_PLAYLISTS = "daily-jams,weekly-jams,weekly-exploration"
DEFAULT_SCHEDULE = "0 */6 * * *"
GENERATED_SIZE = 50
GENERATED_MODES = ("familiar", "discover", "score")
SERIES = {
    "weekly-exploration": {"name": "Weekly Exploration", "previous": "Last Week's", "unit": "week"},
    "weekly-jams": {"name": "Weekly Jams", "previous": "Last Week's", "unit": "week"},
    "daily-jams": {"name": "Daily Exploration", "previous": "Yesterday's", "unit": "day"},
}
UNAVAILABLE_HINTS = {
    "daily-jams": "Not on ListenBrainz yet. Follow troi-bot on ListenBrainz to get Daily Jams.",
}
# (weekday or None for every day, start hour, end hour) in local time, when ListenBrainz usually publishes
WATCH_WINDOWS = {"weekly-exploration": (0, 0, 13), "weekly-jams": (0, 0, 13), "daily-jams": (None, 0, 10)}
CONFIG_KEYS = (
    "LISTENBRAINZ_USER", "LISTENBRAINZ_TOKEN", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REDIRECT_URI", "SYNC_PLAYLISTS", "PLAYLIST_PREFIX", "PLAYLIST_PUBLIC", "CRON_SCHEDULE",
    "GENERATED_ENABLED", "GENERATED_NAME", "GENERATED_EXCLUDE_DAYS", "GENERATED_ARTIST_LIMIT",
    "GENERATED_MODE", "SKIP_HATED", "WATCH_ENABLED", "GENERATED_PUBLIC",
)
DEFAULTS = {
    "SYNC_PLAYLISTS": DEFAULT_PLAYLISTS,
    "PLAYLIST_PREFIX": "",
    "SERIES_OPTIONS": {},
    "WATCH_ENABLED": "true",
    "PLAYLIST_PUBLIC": "false",
    "GENERATED_PUBLIC": "false",
    "CRON_SCHEDULE": DEFAULT_SCHEDULE,
    "GENERATED_ENABLED": "false",
    "GENERATED_EXCLUDE_DAYS": "30",
    "GENERATED_ARTIST_LIMIT": "2",
    "GENERATED_MODE": "familiar",
    "SKIP_HATED": "true",
    "EXTRA_PLAYLISTS": [],
}

log = logging.getLogger("lb-spotify-sync")


class SetupError(Exception):
    """A problem the user has to fix on the setup page; stops the whole sync."""


class ServiceError(Exception):
    """A temporary or per-playlist problem talking to Spotify or ListenBrainz."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def api_message(resp):
    try:
        body = resp.json()
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message") or str(resp.status_code)
        return body.get("error_description") or err or body.get("message") or str(resp.status_code)
    except ValueError:
        return resp.text[:120] or str(resp.status_code)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    tmp.replace(path)


def write_json(path, data, private=False):
    write_private(path, json.dumps(data, indent=2, sort_keys=True).encode())


_fernet = None


def fernet():
    global _fernet
    if _fernet is None:
        passphrase = os.environ.get("SECRET_KEY", "").strip()
        if passphrase:
            key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
        else:
            if not KEY_FILE.exists():
                KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                KEY_FILE.parent.chmod(0o700)
                try:
                    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(Fernet.generate_key())
                except FileExistsError:
                    pass
            key = KEY_FILE.read_bytes().strip()
        _fernet = Fernet(key)
    return _fernet


def encrypt(value):
    return ENC_PREFIX + fernet().encrypt(value.encode()).decode()


def decrypt(value):
    if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
        return value
    try:
        return fernet().decrypt(value[len(ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        return None


def read_token():
    if LEGACY_TOKEN_FILE.exists():
        write_token(read_json(LEGACY_TOKEN_FILE, {}))
        LEGACY_TOKEN_FILE.unlink()
    try:
        return json.loads(fernet().decrypt(TOKEN_FILE.read_bytes()))
    except FileNotFoundError:
        return None
    except InvalidToken:
        log.warning("The saved Spotify login can't be decrypted (the encryption key changed). Reconnect Spotify.")
        return None


def write_token(token):
    write_private(TOKEN_FILE, fernet().encrypt(json.dumps(token).encode()))


def delete_token():
    TOKEN_FILE.unlink(missing_ok=True)
    LEGACY_TOKEN_FILE.unlink(missing_ok=True)


def spotify_connected():
    return read_token() is not None


def save_config(updates):
    stored = read_json(CONFIG_FILE, {})
    stored.update({k: encrypt(v) if k in SECRET_KEYS and v else v for k, v in updates.items()})
    write_json(CONFIG_FILE, stored, private=True)


def migrate_secrets():
    """Encrypt secrets saved in plain text by older versions."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stored = read_json(CONFIG_FILE, None)
    if stored and any(isinstance(stored.get(k), str) and stored[k] and not stored[k].startswith(ENC_PREFIX)
                      for k in SECRET_KEYS):
        save_config({k: stored[k] for k in SECRET_KEYS if stored.get(k) and not stored[k].startswith(ENC_PREFIX)})
    read_token()


def load_config(pending=None):
    stored = read_json(CONFIG_FILE, {})
    stored.update(pending or {})
    cfg = {k: os.environ[k] for k in CONFIG_KEYS if os.environ.get(k)}
    cfg.update({k: v for k, v in stored.items() if v not in (None, "")})
    cfg["UNREADABLE_SECRETS"] = []
    for key in SECRET_KEYS:
        if key in cfg:
            value = decrypt(cfg[key])
            if value is None:
                cfg.pop(key)
                cfg["UNREADABLE_SECRETS"].append(key)
            else:
                cfg[key] = value
    for key, value in DEFAULTS.items():
        cfg.setdefault(key, value)
    return cfg


def selected_patches(cfg):
    return {p.strip() for p in cfg["SYNC_PLAYLISTS"].split(",") if p.strip() and p.strip() != "none"}


def with_prefix(cfg, name):
    return f"{cfg['PLAYLIST_PREFIX'].strip()} {name}".strip()


def generated_name(cfg):
    return cfg.get("GENERATED_NAME") or with_prefix(cfg, "Generated Jams")


def series_base_name(cfg, patch):
    return (series_options(cfg, patch).get("name") or "").strip() or SERIES[patch]["name"]


def patch_playlist_name(cfg, patch):
    return with_prefix(cfg, series_base_name(cfg, patch) if patch in SERIES else pretty_name(patch))


def previous_name(cfg, patch):
    return with_prefix(cfg, f"{SERIES[patch]['previous']} {series_base_name(cfg, patch)}")


def archive_name(cfg, patch, period):
    return f"{patch_playlist_name(cfg, patch)} - {period}"


def series_options(cfg, patch):
    opts = {"previous": True, "archive_days": 0, "public": False}
    opts.update((cfg.get("SERIES_OPTIONS") or {}).get(patch, {}))
    return opts


def series_wanted(cfg, patch):
    opts = series_options(cfg, patch)
    return patch in selected_patches(cfg) or opts["previous"] or opts["archive_days"] != 0


def redirect_uri(cfg):
    return cfg.get("SPOTIFY_REDIRECT_URI") or f"http://127.0.0.1:{os.environ.get('WEB_PORT', '8888')}/callback"


def is_true(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def pretty_name(patch):
    words = re.split(r"[-_]", patch)
    small = {"of", "for", "in", "the", "and"}
    return " ".join("LB" if w == "lb" else w if i and w in small else w.capitalize() for i, w in enumerate(words))


def period_of(pl):
    match = re.search(r"\d{4}-\d{2}-\d{2}", pl.get("title", ""))
    return match.group(0) if match else (pl.get("date") or "")[:10]


def parse_playlist_id(text):
    match = UUID_RE.search(text or "")
    return match.group(0).lower() if match else None


def digest(items):
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:16]


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def track_key(artist, title):
    return f"{normalize(artist)}|{normalize(title)}"


# ---------- ListenBrainz ----------

def lb_session(cfg):
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    if cfg.get("LISTENBRAINZ_TOKEN"):
        session.headers["Authorization"] = f"Token {cfg['LISTENBRAINZ_TOKEN']}"
    return session


def lb_request(session, method, path, **kwargs):
    for attempt in range(6):
        try:
            resp = session.request(method, f"{LB_API}{path}", timeout=60, **kwargs)
        except requests.RequestException:
            if attempt == 5:
                raise ServiceError("Couldn't reach ListenBrainz. It will try again on the next run.")
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 500:
            if attempt == 5:
                raise ServiceError(f"ListenBrainz is having problems ({resp.status_code}). It will try again on the next run.")
            time.sleep(2 ** attempt)
            continue
        reset_in = int(resp.headers.get("X-RateLimit-Reset-In", 5)) + 1
        if resp.status_code == 429:
            log.warning("ListenBrainz rate limit hit, waiting %ss", reset_in)
            time.sleep(reset_in)
            continue
        if resp.status_code == 404 and "/user/" in path:
            raise SetupError("ListenBrainz user not found. Check the username.")
        if resp.status_code == 404 and path.startswith("/1/playlist/"):
            raise ServiceError("This ListenBrainz playlist no longer exists, or it's private.", 404)
        if resp.status_code == 401:
            raise SetupError("ListenBrainz rejected your user token. Check it in step 1, or clear it.")
        if not resp.ok:
            raise ServiceError(f"ListenBrainz error ({resp.status_code}): {api_message(resp)}", resp.status_code)
        if int(resp.headers.get("X-RateLimit-Remaining", 99)) <= 2:
            time.sleep(reset_in)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()
    raise ServiceError("ListenBrainz is busy right now. It will try again on the next run.")


def lb_get(session, path, **params):
    return lb_request(session, "GET", path, params=params)


def mbid_from_url(url):
    if isinstance(url, list):
        url = url[0] if url else ""
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


def generated_playlists_by_patch(session, user):
    data = lb_get(session, f"/1/user/{user}/playlists/createdfor", count=100)
    by_patch = {}
    for item in data.get("playlists", []):
        pl = item["playlist"]
        meta = pl.get("extension", {}).get(JSPF_PLAYLIST_EXT, {})
        patch = meta.get("additional_metadata", {}).get("algorithm_metadata", {}).get("source_patch")
        if patch:
            by_patch.setdefault(patch, []).append(pl)
    for playlists in by_patch.values():
        playlists.sort(key=lambda p: p.get("date", ""), reverse=True)
    return by_patch


def latest_generated_playlists(session, user, wanted=None):
    return {patch: pls[0] for patch, pls in generated_playlists_by_patch(session, user).items()
            if wanted is None or patch in wanted}


def get_playlist(session, playlist_mbid):
    return lb_get(session, f"/1/playlist/{playlist_mbid}")["playlist"]


def playlist_tracks(pl):
    tracks = []
    for t in pl.get("track", []):
        meta = t.get("extension", {}).get(JSPF_TRACK_EXT, {}).get("additional_metadata", {})
        artists = meta.get("artists") or []
        primary = artists[0].get("artist_credit_name") if artists else None
        tracks.append({
            "mbid": mbid_from_url(t.get("identifier")),
            "title": t.get("title") or "",
            "artist": t.get("creator") or "",
            "primary_artist": primary or t.get("creator") or "",
        })
    return tracks


def lookup_metadata(session, mbids):
    result = {}
    for i in range(0, len(mbids), 200):
        chunk = mbids[i:i + 200]
        result.update(lb_request(session, "POST", "/1/metadata/recording/",
                                 json={"recording_mbids": chunk, "inc": "artist release"}) or {})
    return result


def apply_metadata(track, md):
    recording = md.get("recording") or {}
    artists = (md.get("artist") or {}).get("artists") or []
    track.setdefault("title", recording.get("name") or "")
    track.setdefault("artist", (md.get("artist") or {}).get("name") or "")
    track.setdefault("primary_artist", artists[0].get("name") if artists else track["artist"])
    track["isrcs"] = recording.get("isrcs") or []
    track["artist_ids"] = [a.get("artist_mbid") or normalize(a.get("name")) for a in artists] or [normalize(track["artist"])]
    for rel in recording.get("url_rels") or []:
        match = SPOTIFY_LINK_RE.search(rel.get("url", ""))
        if match:
            track["spotify_id"] = match.group(1)
            break
    return track


def hated_mbids(session, user):
    hated, offset = set(), 0
    while True:
        data = lb_get(session, f"/1/feedback/user/{user}/get-feedback", score=-1, count=1000, offset=offset, metadata="false")
        feedback = data.get("feedback", [])
        hated.update(f["recording_mbid"] for f in feedback if f.get("recording_mbid"))
        if len(feedback) < 1000:
            return hated
        offset += 1000


def recent_listens(session, user, cutoff):
    mbids, keys = set(), set()
    max_ts = int(time.time()) + 60
    for _ in range(25):
        listens = lb_get(session, f"/1/user/{user}/listens", count=1000, max_ts=max_ts).get("payload", {}).get("listens", [])
        if not listens:
            break
        for listen in listens:
            if listen["listened_at"] < cutoff:
                continue
            md = listen.get("track_metadata") or {}
            mbid = (md.get("mbid_mapping") or {}).get("recording_mbid") or (md.get("additional_info") or {}).get("recording_mbid")
            if mbid:
                mbids.add(mbid)
            keys.add(track_key(md.get("artist_name"), md.get("track_name")))
        oldest = min(listen["listened_at"] for listen in listens)
        if oldest < cutoff:
            break
        max_ts = oldest
    return mbids, keys


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


# ---------- Spotify ----------

def _token_request(cfg, data):
    try:
        resp = requests.post(
            f"{SPOTIFY_ACCOUNTS}/api/token",
            data=data,
            auth=(cfg["SPOTIFY_CLIENT_ID"], cfg["SPOTIFY_CLIENT_SECRET"]),
            timeout=30,
        )
    except requests.RequestException:
        raise ServiceError("Couldn't reach Spotify. It will try again on the next run.")
    if resp.status_code >= 500:
        raise ServiceError(f"Spotify is having problems ({resp.status_code}). It will try again on the next run.")
    if resp.status_code != 200:
        msg = api_message(resp)
        if "invalid_client" in resp.text or "Invalid client" in msg:
            raise SetupError("Spotify didn't accept your Client ID or secret. Check them in step 2.")
        raise SetupError(f"Spotify rejected the login ({msg}). Click 'Reconnect Spotify' in step 2.")
    token = resp.json()
    token["expires_at"] = time.time() + token["expires_in"] - 60
    return token


def authorize_url(cfg, state):
    return f"{SPOTIFY_ACCOUNTS}/authorize?" + urlencode({
        "client_id": cfg["SPOTIFY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": redirect_uri(cfg),
        "scope": SCOPES,
        "state": state,
    })


def exchange_code(cfg, code):
    token = _token_request(cfg, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(cfg)})
    write_token(token)


class Spotify:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = read_token()
        if not self.token:
            raise SetupError("Spotify is not connected yet. Open the setup page and click 'Connect Spotify'.")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _access_token(self):
        if time.time() >= self.token.get("expires_at", 0):
            new = _token_request(self.cfg, {"grant_type": "refresh_token", "refresh_token": self.token["refresh_token"]})
            new.setdefault("refresh_token", self.token["refresh_token"])
            new.setdefault("scope", self.token.get("scope", ""))
            self.token = new
            write_token(new)
        return self.token["access_token"]

    def has_scope(self, scope):
        return scope in self.token.get("scope", "").split()

    def request(self, method, path, **kwargs):
        for attempt in range(6):
            try:
                resp = self.session.request(
                    method, f"{SPOTIFY_API}{path}",
                    headers={"Authorization": f"Bearer {self._access_token()}"},
                    timeout=30, **kwargs,
                )
            except requests.RequestException:
                if attempt == 5:
                    raise ServiceError("Couldn't reach Spotify. It will try again on the next run.")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401 and attempt == 0:
                self.token["expires_at"] = 0
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                if wait > 300:
                    raise SetupError(f"Spotify's usage limit for your app was reached. It will work again in about {wait // 60} minutes.")
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                raise SetupError("Spotify signed you out. Click 'Reconnect Spotify' in step 2.")
            if resp.status_code == 403:
                msg = api_message(resp)
                if path == "/me" or "user may not be registered" in msg.lower():
                    raise SetupError(
                        "Spotify refused access. If this Spotify account didn't create the developer app, "
                        "add it under 'User Management' in the Spotify dashboard. The app owner also needs Spotify Premium."
                    )
                if "scope" in msg.lower():
                    raise SetupError("Spotify needs new permissions. Click 'Reconnect Spotify' in step 2 and approve again.")
                raise ServiceError(f"Spotify refused this action ({msg}).", 403)
            if not resp.ok:
                raise ServiceError(f"Spotify error ({resp.status_code}): {api_message(resp)}", resp.status_code)
            return resp.json() if resp.content else None
        raise ServiceError("Spotify kept failing. It will try again on the next run.")

    def me(self):
        return self.request("GET", "/me")

    def search_tracks(self, query, limit):
        params = {"q": query, "type": "track", "limit": limit}
        # Restricting results to the user's country needs the user-read-private permission
        if self.has_scope("user-read-private"):
            params["market"] = "from_token"
        return self.request("GET", "/search", params=params)["tracks"]["items"]

    def my_playlists(self):
        playlists, offset = [], 0
        while True:
            page = self.request("GET", "/me/playlists", params={"limit": 50, "offset": offset})
            playlists.extend(p for p in page["items"] if p)
            if not page.get("next"):
                return playlists
            offset += 50

    def create_playlist(self, name, public, description):
        body = {"name": name, "public": public, "description": description}
        return self.request("POST", "/me/playlists", json=body)["id"]

    def replace_items(self, playlist_id, uris):
        self.request("PUT", f"/playlists/{playlist_id}/items", json={"uris": uris[:100]})
        for i in range(100, len(uris), 100):
            self.request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + 100]})

    def set_description(self, playlist_id, description):
        self.request("PUT", f"/playlists/{playlist_id}", json={"description": description})

    def delete_playlist(self, playlist_id):
        try:
            self.request("DELETE", "/me/library", params={"uris": f"spotify:playlist:{playlist_id}"})
        except ServiceError:
            self.request("DELETE", f"/playlists/{playlist_id}/followers")


def artist_matches(item, artist):
    wanted = normalize(artist)
    if not wanted:
        return False
    return any(normalize(a["name"]) in wanted or wanted in normalize(a["name"]) for a in item["artists"])


def find_spotify_track(sp, track):
    for isrc in track.get("isrcs", [])[:2]:
        items = sp.search_tracks(f"isrc:{isrc}", limit=1)
        if items:
            return items[0]["uri"]
    if track.get("spotify_id"):
        return f"spotify:track:{track['spotify_id']}"
    title = track["title"].replace('"', "")
    artist = track["primary_artist"].replace('"', "")
    if not title:
        return None
    items = sp.search_tracks(f'track:"{title}" artist:"{artist}"', limit=5)
    if items:
        return items[0]["uri"]
    for item in sp.search_tracks(f"{title} {artist}", limit=10):
        if artist_matches(item, track["artist"]):
            return item["uri"]
    return None


def resolve_track(ctx, track):
    uri = ctx.cache.get(track["mbid"]) if track.get("mbid") else None
    if not uri:
        try:
            uri = find_spotify_track(ctx.sp, track)
        except ServiceError as e:
            if e.status not in (400, 404):
                raise
            log.warning("Skipping %s - %s: Spotify couldn't search for it (%s)", track.get("artist"), track.get("title"), e)
            return None
        if uri and track.get("mbid"):
            ctx.cache[track["mbid"]] = uri
    return uri


def resolve_uris(ctx, tracks, label):
    tracks = [t for t in tracks if t["mbid"] not in ctx.hated]
    need_meta = [t["mbid"] for t in tracks if t["mbid"] and t["mbid"] not in ctx.cache]
    if need_meta:
        meta = lookup_metadata(ctx.session, need_meta)
        for t in tracks:
            if t["mbid"] in meta:
                apply_metadata(t, meta[t["mbid"]])
    uris = []
    for t in tracks:
        uri = resolve_track(ctx, t)
        if uri:
            if uri not in uris:
                uris.append(uri)
        else:
            log.info("%s: not found on Spotify: %s - %s", label, t["artist"], t["title"])
    return uris


def ensure_playlist(ctx, key, name, description, public):
    pid = ctx.state["playlists"].get(key)
    if pid not in ctx.own:
        match = next((p for p in ctx.own.values() if p["name"] == name), None)
        if match:
            pid = match["id"]
            log.info("Using existing Spotify playlist '%s'", name)
        else:
            pid = ctx.sp.create_playlist(name, public, description)
            ctx.own[pid] = {"id": pid, "name": name, "public": public}
            log.info("Created %s Spotify playlist '%s'", "public" if public else "private", name)
        ctx.state["playlists"][key] = pid
    update_details(ctx, pid, name=name, public=public)
    return pid


def update_details(ctx, pid, name=None, public=None):
    current = ctx.own[pid]
    changes = {}
    if name is not None and current.get("name") != name:
        changes["name"] = name
    if public is not None and current.get("public") is not None and current["public"] != public:
        changes["public"] = public
    if changes:
        ctx.sp.request("PUT", f"/playlists/{pid}", json=changes)
        if "name" in changes:
            log.info("Renamed Spotify playlist '%s' to '%s'", current["name"], name)
        if "public" in changes:
            log.info("Made '%s' %s", name or current["name"], "public" if public else "private")
        current.update(changes)


def up_to_date(ctx, key, marker, name):
    if not ctx.force and ctx.state["synced"].get(key) == marker:
        log.info("%s: up to date", name)
        ctx.results.append({"name": name, "status": "up to date"})
        return True
    return False


def write_playlist(ctx, key, name, pid, description, uris, total, marker):
    ctx.sp.replace_items(pid, uris)
    try:
        ctx.sp.set_description(pid, description[:300])
    except ServiceError as e:
        log.warning("%s: couldn't update the description: %s", name, e)
    ctx.state["synced"][key] = marker
    log.info("%s: synced %d/%d tracks", name, len(uris), total)
    ctx.results.append({"name": name, "status": f"{len(uris)} of {total} tracks"})


def sync_made_for_you(ctx, patch, lb_pl):
    name = patch_playlist_name(ctx.cfg, patch)
    description = f'Auto-synced from ListenBrainz: {lb_pl.get("title", patch)}'
    pid = ensure_playlist(ctx, patch, name, description, is_true(ctx.cfg["PLAYLIST_PUBLIC"]))
    marker = f"{mbid_from_url(lb_pl['identifier'])}:{pid}"
    if up_to_date(ctx, patch, marker, name):
        return
    tracks = playlist_tracks(get_playlist(ctx.session, mbid_from_url(lb_pl["identifier"])))
    uris = resolve_uris(ctx, tracks, name)
    write_playlist(ctx, patch, name, pid, description, uris, len(tracks), marker)


def snapshot(ctx, patch, lb_pl):
    mbid = mbid_from_url(lb_pl["identifier"])
    tracks = playlist_tracks(get_playlist(ctx.session, mbid))
    uris = resolve_uris(ctx, tracks, patch_playlist_name(ctx.cfg, patch))
    return {"period": period_of(lb_pl), "lb": mbid, "title": lb_pl.get("title", ""), "uris": uris, "total": len(tracks)}


def push_entry(ctx, key, name, entry, description, public):
    pid = ensure_playlist(ctx, key, name, description, public)
    marker = f"{entry['lb']}:{pid}"
    if up_to_date(ctx, key, marker, name):
        return
    write_playlist(ctx, key, name, pid, description, entry["uris"], entry["total"], marker)


def archive_cutoff(days):
    return (date.today() - timedelta(days=days)).isoformat() if days > 0 else ""


def sync_series(ctx, patch, entries):
    cfg, info, opts = ctx.cfg, SERIES[patch], series_options(ctx.cfg, patch)
    public = bool(opts.get("public"))
    history = ctx.state["history"].setdefault(patch, [])
    newest = entries[0]
    newest_mbid, newest_period = mbid_from_url(newest["identifier"]), period_of(newest)

    if not history or history[0]["lb"] != newest_mbid:
        if history and newest_period < history[0]["period"]:
            log.warning("%s: ListenBrainz returned an older playlist than the one already synced, ignoring it", patch)
        elif history and newest_period == history[0]["period"]:
            log.info("%s: ListenBrainz regenerated the playlist for %s", patch, newest_period)
            history[0] = snapshot(ctx, patch, newest)
        else:
            if history:
                log.info("%s: new playlist for %s is out", patch, newest_period)
            history.insert(0, snapshot(ctx, patch, newest))
    older = next((e for e in entries[1:] if period_of(e) < history[0]["period"]), None) if history else None
    if older and (len(history) == 1 or period_of(older) > history[1]["period"]):
        history.insert(1, snapshot(ctx, patch, older))

    current = history[0]
    previous = history[1] if len(history) > 1 else None
    source = f'ListenBrainz "{current["title"]}"'
    if patch in ctx.wanted:
        push_entry(ctx, patch, patch_playlist_name(cfg, patch), current, f"Auto-synced from {source}", public)
    if opts["previous"]:
        name = previous_name(cfg, patch)
        if previous:
            push_entry(ctx, f"{patch}:previous", name, previous, f'Auto-synced from ListenBrainz "{previous["title"]}"', public)
        else:
            ctx.results.append({"name": name, "status": f"starts after the next {info['unit']}'s playlist"})

    days = int(opts["archive_days"])
    if days:
        cutoff = archive_cutoff(days)
        for entry in history[1:]:
            key = f"{patch}:archive:{entry['period']}"
            if key in ctx.state["synced"] or entry["period"] < cutoff:
                continue
            push_entry(ctx, key, archive_name(cfg, patch, entry["period"]), entry,
                       f'Archived from ListenBrainz "{entry["title"]}". Part of {patch_playlist_name(cfg, patch)} Archive.',
                       public)
        if days > 0:
            prune_archives(ctx, patch, cutoff)
    prefix = f"{patch}:archive:"
    for key, pid in list(ctx.state["playlists"].items()):
        if key.startswith(prefix) and pid in ctx.own:
            update_details(ctx, pid, name=archive_name(cfg, patch, key[len(prefix):]), public=public)
    del history[2:]


def prune_archives(ctx, patch, cutoff):
    prefix = f"{patch}:archive:"
    for key in [k for k in ctx.state["playlists"] if k.startswith(prefix) and k[len(prefix):] < cutoff]:
        pid = ctx.state["playlists"].pop(key)
        ctx.state["synced"].pop(key, None)
        if pid in ctx.own:
            name = ctx.own.pop(pid)["name"]
            ctx.sp.delete_playlist(pid)
            log.info("Removed '%s' from the archive (older than your archive setting)", name)
            ctx.results.append({"name": name, "status": "removed from archive"})


def sync_extra(ctx, extra):
    key, name = f"id:{extra['id']}", extra["name"]
    pid = ensure_playlist(ctx, key, name, "Auto-synced from ListenBrainz", bool(extra.get("public")))
    if extra.get("one_time") and str(ctx.state["synced"].get(key, "")).endswith(f":{pid}") and not ctx.force:
        log.info("%s: imported once, not updating", name)
        ctx.results.append({"name": name, "status": "imported once"})
        return
    pl = get_playlist(ctx.session, extra["id"])
    tracks = playlist_tracks(pl)
    marker = f"{digest([t['mbid'] or '' for t in tracks])}:{pid}"
    if up_to_date(ctx, key, marker, name):
        return
    description = f'Auto-synced from ListenBrainz playlist "{pl.get("title", "")}" by {pl.get("creator", "unknown")}'
    uris = resolve_uris(ctx, tracks, name)
    write_playlist(ctx, key, name, pid, description, uris, len(tracks), marker)


def generated_uris(ctx):
    cfg = ctx.cfg
    days = max(0, int(cfg["GENERATED_EXCLUDE_DAYS"] or 0))
    artist_limit = max(0, int(cfg["GENERATED_ARTIST_LIMIT"] or 0))
    payload = lb_get(ctx.session, f"/1/cf/recommendation/user/{cfg['LISTENBRAINZ_USER']}/recording", count=1000).get("payload", {})
    recs = payload.get("mbids", [])
    if not recs:
        return None, 0
    cutoff = time.time() - days * 86400
    recent_mbids, recent_keys = recent_listens(ctx.session, cfg["LISTENBRAINZ_USER"], cutoff) if days else (set(), set())

    heard, unheard, excluded = [], [], 0
    for rec in recs:
        mbid = rec["recording_mbid"]
        last = parse_ts(rec.get("latest_listened_at"))
        if mbid in ctx.hated or (days and (mbid in recent_mbids or (last and last >= cutoff))):
            excluded += 1
            continue
        (heard if last else unheard).append(mbid)
    mode = cfg["GENERATED_MODE"]
    if mode == "familiar":
        order = heard + unheard
    elif mode == "discover":
        order = unheard + heard
    else:
        keep = set(heard + unheard)
        order = [r["recording_mbid"] for r in recs if r["recording_mbid"] in keep]

    uris, artist_counts = [], {}
    for i in range(0, len(order), 100):
        chunk = order[i:i + 100]
        meta = lookup_metadata(ctx.session, chunk)
        for mbid in chunk:
            if mbid not in meta:
                continue
            track = apply_metadata({"mbid": mbid}, meta[mbid])
            if days and track_key(track["artist"], track["title"]) in recent_keys:
                continue
            if artist_limit and any(artist_counts.get(a, 0) >= artist_limit for a in track["artist_ids"]):
                continue
            uri = resolve_track(ctx, track)
            if not uri or uri in uris:
                continue
            uris.append(uri)
            for a in track["artist_ids"]:
                artist_counts[a] = artist_counts.get(a, 0) + 1
            if len(uris) >= GENERATED_SIZE:
                return uris, len(recs)
    return uris, len(recs)


def sync_generated(ctx):
    cfg = ctx.cfg
    name = generated_name(cfg)
    description = (f"Built from your ListenBrainz recommendations. Skips songs played in the last "
                   f"{cfg['GENERATED_EXCLUDE_DAYS']} days, max {cfg['GENERATED_ARTIST_LIMIT']} per artist.")
    uris, total = generated_uris(ctx)
    if uris is None:
        log.warning("%s: ListenBrainz has no recommendations for you yet", name)
        ctx.results.append({"name": name, "status": "no recommendations yet"})
        return
    pid = ensure_playlist(ctx, "generated", name, description, is_true(cfg["GENERATED_PUBLIC"]))
    marker = f"{digest(uris)}:{pid}"
    if up_to_date(ctx, "generated", marker, name):
        return
    write_playlist(ctx, "generated", name, pid, description, uris, GENERATED_SIZE, marker)


# ---------- Sync ----------

def sync(cfg, state, results):
    user = cfg.get("LISTENBRAINZ_USER")
    if not user or not cfg.get("SPOTIFY_CLIENT_ID") or not cfg.get("SPOTIFY_CLIENT_SECRET"):
        raise SetupError("Setup isn't finished. Open the setup page (http://localhost:8888) to complete it.")
    wanted = selected_patches(cfg)
    series = [p for p in SERIES if series_wanted(cfg, p)]
    others = sorted(wanted - set(SERIES))
    extras = [e for e in cfg["EXTRA_PLAYLISTS"] if e.get("enabled", True)]
    generate = is_true(cfg["GENERATED_ENABLED"])
    if not series and not others and not extras and not generate:
        raise SetupError("No playlists selected. Pick at least one on the setup page.")

    session = lb_session(cfg)
    sp = Spotify(cfg)
    user_id = sp.me()["id"]
    state.setdefault("history", {})
    ctx = SimpleNamespace(
        cfg=cfg, state=state, session=session, sp=sp, results=results, cache=state["matches"], wanted=wanted,
        force=is_true(os.environ.get("SYNC_FORCE", "false")),
        own={p["id"]: p for p in sp.my_playlists() if p.get("owner", {}).get("id") == user_id},
        hated=hated_mbids(session, user) if is_true(cfg["SKIP_HATED"]) else set(),
    )
    if ctx.hated:
        log.info("Skipping %d songs you've marked as hated on ListenBrainz", len(ctx.hated))

    jobs = []
    if series or others:
        by_patch = generated_playlists_by_patch(session, user)
        for patch in series + others:
            if patch not in by_patch:
                hint = UNAVAILABLE_HINTS.get(patch, "Not on ListenBrainz yet.")
                log.info("%s: %s", patch_playlist_name(cfg, patch), hint)
                results.append({"name": patch_playlist_name(cfg, patch), "status": hint, "level": "info"})
            elif patch in SERIES:
                jobs.append((patch_playlist_name(cfg, patch), lambda p=patch: sync_series(ctx, p, by_patch[p])))
            else:
                jobs.append((patch_playlist_name(cfg, patch), lambda p=patch: sync_made_for_you(ctx, p, by_patch[p][0])))
    if generate:
        jobs.append((generated_name(cfg), lambda: sync_generated(ctx)))
    jobs += [(e["name"], lambda e=e: sync_extra(ctx, e)) for e in extras]

    ok, seen = True, set()
    for name, job in jobs:
        if name in seen:
            log.error("%s: two playlists share this name, skipping the second one", name)
            results.append({"name": name, "status": "Skipped: another playlist has the same name.", "level": "error"})
            ok = False
            continue
        seen.add(name)
        try:
            job()
        except SetupError:
            raise
        except ServiceError as e:
            ok = False
            results.append({"name": name, "status": str(e), "level": "error"})
            log.error("%s: %s", name, e)
        except Exception as e:
            ok = False
            results.append({"name": name, "status": "Something unexpected went wrong. See the log below.", "level": "error"})
            log.exception("%s: unexpected error: %s", name, e)
        finally:
            write_json(STATE_FILE, state)
    return ok


def expected_period(patch, now):
    today = now.date()
    if WATCH_WINDOWS[patch][0] is None:
        return today.isoformat()
    return (today - timedelta(days=today.weekday())).isoformat()


def watch():
    cfg = load_config()
    if not is_true(cfg["WATCH_ENABLED"]) or not cfg.get("LISTENBRAINZ_USER") or not spotify_connected():
        return 0
    now = datetime.now().astimezone()
    history = read_json(STATE_FILE, {}).get("history", {})
    pending = []
    for patch, (weekday, start, end) in WATCH_WINDOWS.items():
        if not series_wanted(cfg, patch) or (weekday is not None and now.weekday() != weekday):
            continue
        if not start <= now.hour < end:
            continue
        hist = history.get(patch) or []
        if hist and hist[0]["period"] >= expected_period(patch, now):
            continue
        pending.append(patch)
    if not pending:
        return 0
    try:
        by_patch = generated_playlists_by_patch(lb_session(cfg), cfg["LISTENBRAINZ_USER"])
    except Exception:
        return 0
    changed = [p for p in pending if p in by_patch
               and mbid_from_url(by_patch[p][0]["identifier"]) != (history.get(p) or [{}])[0].get("lb")]
    if not changed:
        return 0
    print(f"New ListenBrainz playlist out ({', '.join(changed)}), syncing now", flush=True)
    return run_sync()


def main():
    if "--watch" in sys.argv[1:]:
        return watch()
    return run_sync()


def run_sync():
    migrate_secrets()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, mode="w")]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

    state = read_json(STATE_FILE, {})
    for key in ("playlists", "synced", "matches"):
        state.setdefault(key, {})
    results, error = [], None
    try:
        if not sync(load_config(), state, results):
            error = "Some playlists couldn't be synced. See the details below."
    except (SetupError, ServiceError) as e:
        error = str(e)
        log.error(error)
    except Exception as e:
        error = "Something unexpected went wrong. The log below has the details."
        log.exception("Sync failed: %s", e)
    state["last_run"] = {"time": datetime.now(timezone.utc).isoformat(), "ok": error is None,
                         "error": error, "playlists": results}
    write_json(STATE_FILE, state)
    return 0 if error is None else 1


if __name__ == "__main__":
    sys.exit(main())
