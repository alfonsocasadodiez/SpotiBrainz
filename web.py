import base64
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import sync

PORT = int(os.environ.get("WEB_PORT", "8888"))
PASSWORD = os.environ.get("WEB_PASSWORD", "")
SYNC_SCRIPT = str(Path(__file__).with_name("sync.py"))
SCHEDULES = [
    ("0 * * * *", "Every hour"),
    ("0 */6 * * *", "Every 6 hours (recommended)"),
    ("0 7 * * *", "Every day at 07:00"),
    ("0 7 * * 1", "Every Monday at 07:00"),
]
SERIES_UI = {
    "weekly-exploration": ("Music that's new to you, every Monday", "This week's playlist", "Last week's playlist"),
    "weekly-jams": ("Music you already love, every Monday", "This week's playlist", "Last week's playlist"),
    "daily-jams": ("A fresh mix every day, from ListenBrainz Daily Jams", "Today's playlist", "Yesterday's playlist"),
}
ARCHIVE_OPTIONS = [("0", "Off"), ("7", "Keep 1 week"), ("30", "Keep 1 month"), ("91", "Keep 3 months"),
                   ("182", "Keep 6 months"), ("365", "Keep 1 year"), ("-1", "Keep forever")]
EXCLUDE_DAYS = [("0", "Don't skip any"), ("7", "7 days"), ("14", "14 days"), ("30", "30 days"),
                ("60", "60 days"), ("90", "90 days"), ("180", "180 days")]
ARTIST_LIMITS = [("1", "1"), ("2", "2"), ("3", "3"), ("5", "5"), ("0", "No limit")]
MODES = [("familiar", "Songs I know but haven't played lately"),
         ("discover", "Songs that are new to me"),
         ("score", "Best matches, new or not")]
CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-]+$")

pending_states = {}
lb_cache = {}
sync_proc = None


def save_config(updates):
    sync.save_config(updates)


def install_crontab(schedule):
    job = f". /app/env.sh; cd /app && flock -n /tmp/sync.lock python {SYNC_SCRIPT}"
    lines = f"{schedule} {job} >> /proc/1/fd/1 2>&1\n*/10 * * * * {job} --watch >> /proc/1/fd/1 2>&1\n"
    try:
        subprocess.run(["crontab", "-"], input=lines, text=True, check=True, capture_output=True)
    except FileNotFoundError:
        print("crontab not available, scheduling disabled", flush=True)
        return True
    except subprocess.CalledProcessError:
        return False
    return True


def sync_running():
    return sync_proc is not None and sync_proc.poll() is None


def start_sync():
    global sync_proc
    if sync_running():
        return
    sync_proc = subprocess.Popen(["flock", "-n", "/tmp/sync.lock", sys.executable, SYNC_SCRIPT])


def is_ready(cfg):
    return all(cfg.get(k) for k in ("LISTENBRAINZ_USER", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")) \
        and sync.spotify_connected()


def lb_playlist_types(cfg):
    user = cfg.get("LISTENBRAINZ_USER")
    if not user:
        return None, None
    key = (user, cfg.get("LISTENBRAINZ_TOKEN"))
    cached = lb_cache.get(key)
    if cached and time.time() - cached[0] < 300:
        return cached[1], None
    try:
        found = sync.latest_generated_playlists(sync.lb_session(cfg), user)
    except sync.SetupError as e:
        return None, str(e)
    except Exception as e:
        return None, f"Couldn't reach ListenBrainz: {e}"
    types = sorted(found)
    lb_cache[key] = (time.time(), types)
    return types, None


def taken_names(cfg, include_generated=True):
    names = {sync.patch_playlist_name(cfg, p) for p in sync.selected_patches(cfg)}
    names |= {sync.previous_name(cfg, p) for p in sync.SERIES if sync.series_options(cfg, p)["previous"]}
    if include_generated and sync.is_true(cfg["GENERATED_ENABLED"]):
        names.add(sync.generated_name(cfg))
    return names | {e["name"] for e in cfg["EXTRA_PLAYLISTS"]}


def duplicate_name(cfg):
    selected = sync.selected_patches(cfg)
    names = [sync.patch_playlist_name(cfg, p) for p in sorted(selected)]
    names += [sync.previous_name(cfg, p) for p in sync.SERIES if sync.series_options(cfg, p)["previous"]]
    if sync.is_true(cfg["GENERATED_ENABLED"]):
        names.append(sync.generated_name(cfg))
    names += [e["name"] for e in cfg["EXTRA_PLAYLISTS"]]
    seen = set()
    for name in names:
        if name.casefold() in seen:
            return name
        seen.add(name.casefold())
    return None


def local_time(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "never"


def pill(done, label_done="Done", label_todo="To do"):
    return f'<span class="pill {"ok" if done else "todo"}">{label_done if done else label_todo}</span>'


def select(name, current, options):
    if current not in [value for value, _ in options]:
        options = options + [(current, current)]
    opts = "".join(f'<option value="{escape(value)}"{" selected" if value == current else ""}>{escape(label)}</option>'
                   for value, label in options)
    return f'<select id="{name}" name="{name}">{opts}</select>'


def render(cfg, msg="", kind="ok"):
    state = sync.read_json(sync.STATE_FILE, {})
    last = state.get("last_run") or {}
    types, lb_error = lb_playlist_types(cfg)
    connected = sync.spotify_connected()
    has_creds = bool(cfg.get("SPOTIFY_CLIENT_ID") and cfg.get("SPOTIFY_CLIENT_SECRET"))
    ready = is_ready(cfg)
    uri = sync.redirect_uri(cfg)
    selected = sync.selected_patches(cfg)
    extras = cfg["EXTRA_PLAYLISTS"]
    generate = sync.is_true(cfg["GENERATED_ENABLED"])
    log_text = sync.LOG_FILE.read_text()[-8000:] if sync.LOG_FILE.exists() else ""
    v = lambda k: escape(cfg.get(k, ""), quote=True)

    banner = f'<div class="banner {escape(kind)}">{escape(msg)}</div>' if msg else ""
    if cfg["UNREADABLE_SECRETS"]:
        banner += ('<div class="banner err">Your saved keys can\'t be read because the encryption key changed. '
                   'Please enter them again in steps 1 and 2.</div>')

    if sync_running():
        status = '<span class="pill todo">Syncing now...</span>'
    elif not ready:
        status = '<span class="pill todo">Setup not finished</span>'
    elif last and not last.get("ok"):
        status = '<span class="pill err">Last sync had problems</span>'
    else:
        status = '<span class="pill ok">Active</span>'
    last_line = f"Last sync: {local_time(last.get('time'))}"
    if last.get("error"):
        last_line += f'<br><span class="errtext">{escape(last["error"])}</span>'
    results = "".join(
        f'<li><span class="rname">{escape(r["name"])}</span>'
        f'<span class="rstatus {"errtext" if r.get("level") == "error" or r["status"].startswith("failed") else "muted"}">'
        f'{escape(r["status"])}</span></li>'
        for r in last.get("playlists", [])
    )
    results = f'<ul class="results">{results}</ul>' if results else ""

    if lb_error:
        lb_status = f'<p class="errtext">{escape(lb_error)}</p>'
    elif types is not None and not types:
        lb_status = ('<p class="warn">No playlists yet. Link Spotify in your '
                     '<a href="https://listenbrainz.org/settings/" target="_blank" rel="noopener">ListenBrainz settings</a> '
                     'and check back in a few days.</p>')
    elif types:
        lb_status = f'<p class="muted">Found {len(types)} playlist types for {v("LISTENBRAINZ_USER")}.</p>'
    else:
        lb_status = ""

    available = set(types or [])

    def box(t, note=""):
        return (f'<label class="check"><input type="checkbox" name="playlists" value="{escape(t)}"'
                f'{" checked" if t in selected else ""}> {escape(sync.pretty_name(t))}'
                f'{f" <span class=small-muted>{note}</span>" if note else ""}</label>')

    def series_block(patch):
        blurb, cur_label, prev_label = SERIES_UI[patch]
        opts = sync.series_options(cfg, patch)
        unit = sync.SERIES[patch]["unit"]
        missing = types is not None and patch not in available
        hint = ""
        if missing and patch == "daily-jams":
            hint = ('<p class="hint">Not available yet. Follow '
                    '<a href="https://listenbrainz.org/user/troi-bot/" target="_blank" rel="noopener">troi-bot</a> '
                    'on ListenBrainz to get it.</p>')
        elif missing:
            hint = '<p class="hint">Not available yet.</p>'
        sample_date = "2026-10-19" if unit == "week" else "2026-10-21"
        default = sync.SERIES[patch]["name"]
        word = sync.SERIES[patch]["previous"]
        preview = lambda kind, text: (f'<span class="small-muted">"<span data-preview="{kind}" data-series="{patch}" '
                                      f'data-word="{escape(word)}" data-date="{sample_date}">{escape(text)}</span>"</span>')
        return f"""<div class="series">
      <h3>{escape(default)}</h3><p class="small-muted" style="margin:0 0 4px">{escape(blurb)}</p>
      <div class="name-row"><label for="name_{patch}">Name</label>
        <input id="name_{patch}" class="series-name" type="text" name="name_{patch}" maxlength="80" autocomplete="off"
          value="{escape((opts.get("name") or ""), quote=True)}" placeholder="{escape(default)}" data-series="{patch}" data-default="{escape(default)}"></div>
      <label class="check"><input type="checkbox" name="playlists" value="{patch}"{" checked" if patch in selected else ""}>
        {cur_label} {preview("current", sync.patch_playlist_name(cfg, patch))}</label>
      <label class="check"><input type="checkbox" name="prev_{patch}" value="true"{" checked" if opts["previous"] else ""}>
        {prev_label} {preview("previous", sync.previous_name(cfg, patch))}</label>
      <div class="archive-row"><label for="arch_{patch}">Archive past {unit}s</label>{select(f"arch_{patch}", str(opts["archive_days"]), ARCHIVE_OPTIONS)}</div>
      <p class="small-muted" style="margin:4px 0 0">Saved as {preview("archive", sync.archive_name(cfg, patch, sample_date))}.</p>
      <label class="check"><input type="checkbox" name="pub_{patch}" value="true"{" checked" if opts.get("public") else ""}> Public on Spotify</label>
      {hint}
    </div>"""

    boxes = f"""<div class="prefix-box">
      <label for="pfx">Name prefix <span class="small-muted">(optional)</span></label>
      <input id="pfx" type="text" name="PLAYLIST_PREFIX" value="{v("PLAYLIST_PREFIX")}" placeholder="e.g. LB" maxlength="40" autocomplete="off">
    </div>"""
    boxes += "".join(series_block(p) for p in sync.SERIES)
    boxes += ('<details class="folder-note"><summary>Why aren\'t archives in a folder?</summary><p class="small-muted">'
              'Spotify doesn\'t let apps create folders. Create one in the Spotify app and drag the archived '
              'playlists in.</p></details>')
    others = [t for t in sorted(available) if t not in sync.SERIES]
    if others:
        open_attr = " open" if selected & set(others) else ""
        boxes += (f'<details{open_attr}><summary>{len(others)} more (yearly and one-off playlists)</summary>'
                  + "".join(box(t) for t in others)
                  + f'<label class="check" style="margin-top:10px"><input type="checkbox" name="PLAYLIST_PUBLIC" value="true"'
                  + f'{" checked" if sync.is_true(cfg["PLAYLIST_PUBLIC"]) else ""}> Make these public on Spotify</label></details>')

    def extra_row(e):
        enabled = e.get("enabled", True)
        mode = ("Imported once" if e.get("one_time") else "Kept up to date") if enabled else "Paused"
        mode += " &middot; " + ("Public" if e.get("public") else "Private")
        hidden = f'<input type="hidden" name="id" value="{escape(e["id"])}">'
        return (f'<li class="row"><div><b>{escape(e["name"])}</b><br><span class="small-muted">{mode} &middot; '
                f'<a href="https://listenbrainz.org/playlist/{escape(e["id"])}/" target="_blank" rel="noopener">view on ListenBrainz</a></span></div>'
                f'<div class="row-actions"><form method="post" action="/extra/public">{hidden}'
                f'<button class="secondary small-btn">{"Make private" if e.get("public") else "Make public"}</button></form>'
                f'<form method="post" action="/extra/toggle">{hidden}'
                f'<button class="secondary small-btn">{"Pause" if enabled else "Resume"}</button></form>'
                f'<form method="post" action="/extra/remove">{hidden}<button class="secondary small-btn">Remove</button></form></div></li>')

    extra_rows = "".join(extra_row(e) for e in extras)
    extra_list = f'<ul class="extras">{extra_rows}</ul>' if extras else ""

    current = cfg["CRON_SCHEDULE"]
    preset_values = [s for s, _ in SCHEDULES]
    options = "".join(
        f'<option value="{s}"{" selected" if s == current else ""}>{label}</option>' for s, label in SCHEDULES
    )
    custom_selected = current not in preset_values
    options += f'<option value="custom"{" selected" if custom_selected else ""}>Custom (cron expression)</option>'

    if connected:
        who = escape(cfg.get("SPOTIFY_CONNECTED_AS", "your account"))
        spotify_status = f'<p class="muted">Connected as <b>{who}</b>.</p>'
        granted = (sync.read_token() or {}).get("scope", "").split()
        if "user-read-private" not in granted:
            spotify_status += ('<p class="warn">Click <b>Reconnect Spotify</b> to grant a new permission needed for better song matching.</p>')
        connect_label = "Reconnect Spotify"
    else:
        spotify_status = ""
        connect_label = "Connect Spotify"
    default_gen_name = sync.generated_name({**cfg, "GENERATED_NAME": ""})
    saved_id = cfg.get("SPOTIFY_CLIENT_ID", "")
    saved_id_hint = f"Saved ({saved_id[:4]}…{saved_id[-4:]}). Leave blank to keep it" if len(saved_id) > 8 else \
        ("Saved. Leave blank to keep it" if saved_id else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ListenBrainz to Spotify</title>
<style>
:root {{ --bg:#f6f6f4; --card:#fff; --text:#1d1d1f; --muted:#6b6b70; --border:#e3e3e0; --accent:#1db954; --accent-text:#fff;
  --ok-bg:#e3f6ea; --ok:#11743a; --todo-bg:#fff3d6; --todo:#8a5a00; --err-bg:#fde4e4; --err:#a12020; --code:#f0f0ed; --sub:#fafaf8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#121214; --card:#1c1c1f; --text:#ececee; --muted:#9a9aa2; --border:#2e2e33;
  --ok-bg:#123522; --ok:#6fdc9a; --todo-bg:#3a2e10; --todo:#f2c55c; --err-bg:#3d1717; --err:#ff9a9a; --code:#26262b; --sub:#18181b; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
main {{ max-width:720px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:24px; margin:0 0 4px; }} h2 {{ font-size:18px; margin:0; }} h3 {{ font-size:15px; margin:0 0 4px; }}
.sub {{ color:var(--muted); margin:0 0 24px; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }}
.subcard {{ background:var(--sub); border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-top:16px; }}
.head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }}
.step {{ color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.04em; }}
.pill {{ font-size:12px; font-weight:600; padding:3px 10px; border-radius:99px; white-space:nowrap; }}
.pill.ok {{ background:var(--ok-bg); color:var(--ok); }} .pill.todo {{ background:var(--todo-bg); color:var(--todo); }}
.pill.err {{ background:var(--err-bg); color:var(--err); }}
.banner {{ padding:12px 16px; border-radius:10px; margin-bottom:16px; }}
.banner.ok {{ background:var(--ok-bg); color:var(--ok); }} .banner.err {{ background:var(--err-bg); color:var(--err); }}
label {{ display:block; font-weight:600; margin:12px 0 4px; }}
label.check {{ font-weight:400; display:flex; gap:8px; align-items:center; margin:6px 0; }}
input[type=text], input[type=password], select {{ width:100%; padding:9px 11px; border:1px solid var(--border); border-radius:8px;
  background:var(--bg); color:var(--text); font:inherit; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:0 14px; }}
@media (max-width:560px) {{ .grid {{ grid-template-columns:1fr; }} }}
button, .btn {{ display:inline-block; margin-top:14px; padding:9px 18px; border:0; border-radius:99px; background:var(--accent);
  color:var(--accent-text); font:inherit; font-weight:600; cursor:pointer; text-decoration:none; }}
button.secondary {{ background:transparent; color:var(--text); border:1px solid var(--border); }}
.small-btn {{ margin:0; padding:4px 12px; font-size:13px; }}
.btn.disabled, button:disabled {{ opacity:.45; pointer-events:none; }}
ol {{ padding-left:20px; margin:0; }} li {{ margin:6px 0; }}
ul.results, ul.extras {{ list-style:none; padding:0; margin:12px 0 0; }}
ul.results li {{ display:grid; grid-template-columns:minmax(0, 2fr) minmax(0, 3fr); gap:4px 16px; padding:8px 0; border-top:1px solid var(--border); margin:0; }}
ul.results .rname {{ font-weight:500; overflow-wrap:anywhere; }}
ul.results .rstatus {{ text-align:right; overflow-wrap:anywhere; }}
@media (max-width:560px) {{ ul.results li {{ grid-template-columns:1fr; }} ul.results .rstatus {{ text-align:left; }} }}
.prefix-box {{ padding-bottom:14px; }}
.name-row {{ display:flex; align-items:center; gap:10px; margin:6px 0 4px; }}
.name-row label {{ margin:0; font-weight:400; }}
.name-row input {{ max-width:320px; }}
ul.extras li.row {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:8px 0; border-top:1px solid var(--border); margin:0; }}
.row-actions {{ display:flex; gap:6px; flex-shrink:0; }}
.series {{ border-top:1px solid var(--border); padding:14px 0 10px; }}
.series:first-of-type {{ border-top:0; padding-top:4px; }}
.archive-row {{ display:flex; align-items:center; gap:10px; margin-top:6px; }}
.archive-row label {{ margin:0; font-weight:400; white-space:nowrap; }}
.archive-row select {{ width:auto; flex:1; max-width:220px; }}
.folder-note {{ margin:4px 0 12px; }}
code {{ background:var(--code); padding:2px 6px; border-radius:6px; font-size:13px; word-break:break-all; }}
.copy {{ margin:0 0 0 6px; padding:2px 10px; font-size:12px; }}
.muted {{ color:var(--muted); }} .small-muted {{ color:var(--muted); font-size:13px; font-weight:400; }}
.warn {{ color:var(--todo); }} .errtext {{ color:var(--err); }}
.hint {{ color:var(--muted); font-size:14px; margin:8px 0 0; }}
details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--muted); }}
pre {{ background:var(--code); padding:12px; border-radius:8px; overflow-x:auto; font-size:12px; max-height:320px; white-space:pre-wrap; }}
a {{ color:inherit; }}
</style></head><body><main>
<h1>ListenBrainz to Spotify</h1>
<p class="sub">Keeps your ListenBrainz playlists copied to your Spotify account, automatically.</p>
{banner}

<section class="card">
  <div class="head"><h2>Status</h2>{status}</div>
  <p class="muted" style="margin:0">{last_line}</p>
  {results}
  <form method="post" action="/sync"><button {"" if ready and not sync_running() else "disabled"}>Sync now</button></form>
  {f'<details><summary>Last sync log</summary><pre>{escape(log_text)}</pre></details>' if log_text else ""}
</section>

<section class="card">
  <div class="head"><div><div class="step">Step 1</div><h2>Your ListenBrainz account</h2></div>{pill(bool(types))}</div>
  <form method="post" action="/save/listenbrainz">
    <label for="lbu">ListenBrainz username</label>
    <input id="lbu" type="text" name="LISTENBRAINZ_USER" value="{v("LISTENBRAINZ_USER")}" placeholder="e.g. alice" required autocomplete="off" spellcheck="false" data-1p-ignore data-lpignore="true">
    {lb_status}
    <details><summary>My playlists are private</summary>
      <label for="lbt">ListenBrainz user token</label>
      <input id="lbt" type="password" name="LISTENBRAINZ_TOKEN" placeholder="{"Saved. Leave blank to keep it" if cfg.get("LISTENBRAINZ_TOKEN") else "Find it at listenbrainz.org/settings"}" autocomplete="new-password" spellcheck="false" data-1p-ignore data-lpignore="true">
    </details>
    <button>Save</button>
  </form>
</section>

<section class="card">
  <div class="head"><div><div class="step">Step 2</div><h2>Connect Spotify</h2></div>{pill(connected)}</div>
  <p class="muted" style="margin-top:0">Create a free Spotify developer app to get your keys:</p>
  <ol>
    <li>Open the <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">Spotify Developer Dashboard</a> and log in with your Spotify account. (Spotify requires the account to have Premium.)</li>
    <li>Click <b>Create app</b>. Use any name and description.</li>
    <li>In <b>Redirect URIs</b>, paste this exact address and click <b>Add</b>:<br>
      <code id="uri">{escape(uri)}</code><button type="button" class="copy secondary" onclick="navigator.clipboard.writeText(document.getElementById('uri').textContent);this.textContent='Copied'">Copy</button></li>
    <li>Under "Which API/SDKs are you planning to use?", tick <b>Web API</b>, accept the terms and click <b>Save</b>.</li>
    <li>Copy the <b>Client ID</b> and <b>Client secret</b> (click "View client secret") into the boxes below.</li>
  </ol>
  <form method="post" action="/save/spotify">
    <label for="cid">Client ID</label>
    <input id="cid" type="text" name="SPOTIFY_CLIENT_ID" placeholder="{escape(saved_id_hint) if saved_id_hint else "Paste your Client ID"}" {"" if cfg.get("SPOTIFY_CLIENT_ID") else "required"} autocomplete="off" spellcheck="false" data-1p-ignore data-lpignore="true">
    <label for="cs">Client secret</label>
    <input id="cs" type="password" name="SPOTIFY_CLIENT_SECRET" placeholder="{"Saved. Leave blank to keep it" if cfg.get("SPOTIFY_CLIENT_SECRET") else "Paste your Client secret"}" {"" if cfg.get("SPOTIFY_CLIENT_SECRET") else "required"} autocomplete="new-password" spellcheck="false" data-1p-ignore data-lpignore="true">
    <button>Save keys</button>
  </form>
  <hr style="border:0;border-top:1px solid var(--border);margin:20px 0 6px">
  {spotify_status}
  <a class="btn{"" if has_creds else " disabled"}" href="/connect">{connect_label}</a>
  <details><summary>After approving, the browser showed "This site can't be reached"?</summary>
    <p class="muted">Copy the full address from the address bar and paste it here:</p>
    <form method="post" action="/paste-callback">
      <input type="text" name="url" placeholder="{escape(uri)}?code=..." required autocomplete="off">
      <button>Finish connecting</button>
    </form>
  </details>
</section>

<section class="card">
  <div class="head"><div><div class="step">Step 3</div><h2>Choose your playlists</h2></div>{pill(bool(selected or generate or extras), "Done", "To do")}</div>
  <form method="post" action="/save/playlists">
    {boxes}

    <div class="subcard">
      <label class="check" style="margin-top:0"><input type="checkbox" name="GENERATED_ENABLED" value="true"{" checked" if generate else ""}> <b>Generated Jams</b></label>
      <p class="small-muted" style="margin:0">50 songs picked from your ListenBrainz recommendations.</p>
      <div class="grid">
        <div><label for="gname">Playlist name</label>
          <input id="gname" type="text" name="GENERATED_NAME" value="{v("GENERATED_NAME")}" placeholder="{escape(default_gen_name)}" maxlength="80" autocomplete="off"></div>
        <div><label for="GENERATED_MODE">Favor</label>{select("GENERATED_MODE", cfg["GENERATED_MODE"], MODES)}</div>
        <div><label for="GENERATED_EXCLUDE_DAYS">Skip songs I played in the last</label>{select("GENERATED_EXCLUDE_DAYS", cfg["GENERATED_EXCLUDE_DAYS"], EXCLUDE_DAYS)}</div>
        <div><label for="GENERATED_ARTIST_LIMIT">Max songs per artist</label>{select("GENERATED_ARTIST_LIMIT", cfg["GENERATED_ARTIST_LIMIT"], ARTIST_LIMITS)}</div>
      </div>
      <label class="check"><input type="checkbox" name="GENERATED_PUBLIC" value="true"{" checked" if sync.is_true(cfg["GENERATED_PUBLIC"]) else ""}> Public on Spotify</label>
    </div>

    <label class="check" style="margin-top:16px"><input type="checkbox" name="SKIP_HATED" value="true"{" checked" if sync.is_true(cfg["SKIP_HATED"]) else ""}> Leave out songs I've marked as "hated" on ListenBrainz</label>
    <button>Save</button>
  </form>

  <div class="subcard">
    <h3>Any other ListenBrainz playlist</h3>
    {extra_list}
    <form method="post" action="/extra/add">
      <label for="xurl">Playlist link</label>
      <input id="xurl" type="text" name="url" placeholder="https://listenbrainz.org/playlist/..." required autocomplete="off">
      <label for="xname">Name on Spotify <span class="small-muted">(optional)</span></label>
      <input id="xname" type="text" name="name" autocomplete="off">
      <label class="check"><input type="checkbox" name="keep_updated" value="true" checked> Keep it up to date</label>
      <label class="check"><input type="checkbox" name="public" value="true"> Public on Spotify</label>
      <button>Add playlist</button>
    </form>
  </div>
</section>

<section class="card">
  <div class="head"><div><div class="step">Step 4</div><h2>Schedule</h2></div><span class="pill ok">Optional</span></div>
  <form method="post" action="/save/schedule">
    <label for="sched">Check for new playlists</label>
    <select id="sched" name="schedule" onchange="document.getElementById('customwrap').style.display=this.value==='custom'?'block':'none'">{options}</select>
    <div id="customwrap" style="display:{"block" if custom_selected else "none"}">
      <label for="cron">Cron expression</label>
      <input id="cron" type="text" name="custom" value="{escape(current) if custom_selected else ""}" placeholder="0 */6 * * *">
    </div>
    <label class="check" style="margin-top:12px"><input type="checkbox" name="WATCH_ENABLED" value="true"{" checked" if sync.is_true(cfg["WATCH_ENABLED"]) else ""}> Grab new playlists the moment they come out</label>
    <p class="small-muted" style="margin:0 0 0 24px">Checks every 10 minutes on Monday mornings and early each morning until the new playlist is out.</p>
    <button>Save</button>
  </form>
</section>
</main>
<script>
(function () {{
  var prefix = document.getElementById("pfx");
  var gname = document.getElementById("gname");
  function withPrefix(name) {{
    var p = prefix ? prefix.value.trim() : "";
    return p ? p + " " + name : name;
  }}
  function baseName(series) {{
    var input = document.querySelector('.series-name[data-series="' + series + '"]');
    return (input.value.trim() || input.dataset.default);
  }}
  function refresh() {{
    document.querySelectorAll("[data-preview]").forEach(function (el) {{
      var base = baseName(el.dataset.series), kind = el.dataset.preview;
      if (kind === "current") el.textContent = withPrefix(base);
      else if (kind === "previous") el.textContent = withPrefix(el.dataset.word + " " + base);
      else if (kind === "archive") el.textContent = withPrefix(base) + " - " + el.dataset.date;
    }});
    if (gname) gname.placeholder = withPrefix("Generated Jams");
  }}
  document.querySelectorAll("#pfx, .series-name").forEach(function (el) {{ el.addEventListener("input", refresh); }});
}})();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "lb-spotify-sync"

    def log_message(self, fmt, *args):
        pass

    def authorized(self):
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                if secrets.compare_digest(given, PASSWORD):
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ListenBrainz to Spotify"')
        self.end_headers()
        return False

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def back(self, msg, kind="ok"):
        self.redirect(f"/?kind={kind}&msg={quote(msg)}")

    def do_GET(self):
        if not self.authorized():
            return
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        cfg = sync.load_config()
        if url.path == "/":
            body = render(cfg, query.get("msg", ""), "err" if query.get("kind") == "err" else "ok").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/connect":
            if not (cfg.get("SPOTIFY_CLIENT_ID") and cfg.get("SPOTIFY_CLIENT_SECRET")):
                return self.back("Save your Spotify Client ID and secret first.", "err")
            state = secrets.token_urlsafe(24)
            pending_states[state] = time.time()
            self.redirect(sync.authorize_url(cfg, state))
        elif url.path == "/callback":
            self.finish_connect(cfg, query)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.authorized():
            return
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send_error(403)
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        get = lambda k: form.get(k, [""])[0].strip()
        cfg = sync.load_config()
        path = urlparse(self.path).path

        if path == "/save/listenbrainz":
            updates = {"LISTENBRAINZ_USER": get("LISTENBRAINZ_USER")}
            if get("LISTENBRAINZ_TOKEN"):
                updates["LISTENBRAINZ_TOKEN"] = get("LISTENBRAINZ_TOKEN")
            save_config(updates)
            types, err = lb_playlist_types(sync.load_config())
            if err:
                return self.back(err, "err")
            self.back("ListenBrainz account saved." + ("" if sync.spotify_connected() else " Next: connect Spotify."))
        elif path == "/save/spotify":
            updates = {k: get(k) for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET") if get(k)}
            if not updates:
                return self.back("Nothing to save. Paste your Client ID and Client secret first.", "err")
            if updates.get("SPOTIFY_CLIENT_ID", cfg.get("SPOTIFY_CLIENT_ID")) != cfg.get("SPOTIFY_CLIENT_ID"):
                # A login from a different developer app won't work with the new keys
                sync.delete_token()
                updates["SPOTIFY_CONNECTED_AS"] = ""
            save_config(updates)
            self.back("Spotify keys saved. Now click 'Connect Spotify'." if not sync.spotify_connected() else "Spotify keys saved.")
        elif path == "/save/playlists":
            self.save_playlists(cfg, form, get)
        elif path == "/save/schedule":
            schedule = get("custom") if get("schedule") == "custom" else get("schedule")
            fields = schedule.split()
            if len(fields) != 5 or not all(CRON_FIELD.match(f) for f in fields):
                return self.back("That schedule isn't a valid 5-field cron expression.", "err")
            schedule = " ".join(fields)
            if not install_crontab(schedule):
                return self.back("That schedule isn't a valid cron expression.", "err")
            save_config({
                "CRON_SCHEDULE": schedule,
                "WATCH_ENABLED": "true" if get("WATCH_ENABLED") else "false",
            })
            self.back("Settings saved.")
        elif path == "/extra/add":
            self.add_extra(cfg, get)
        elif path == "/extra/public":
            extras = [dict(e, public=not e.get("public")) if e["id"] == get("id") else e for e in cfg["EXTRA_PLAYLISTS"]]
            save_config({"EXTRA_PLAYLISTS": extras})
            public = any(e["id"] == get("id") and e["public"] for e in extras)
            self.back(("Made public." if public else "Made private.") + " Spotify updates on the next sync.")
        elif path == "/extra/toggle":
            extras = [dict(e, enabled=not e.get("enabled", True)) if e["id"] == get("id") else e
                      for e in cfg["EXTRA_PLAYLISTS"]]
            save_config({"EXTRA_PLAYLISTS": extras})
            paused = any(e["id"] == get("id") and not e["enabled"] for e in extras)
            self.back("Paused." if paused else "Resumed.")
        elif path == "/extra/remove":
            remaining = [e for e in cfg["EXTRA_PLAYLISTS"] if e["id"] != get("id")]
            save_config({"EXTRA_PLAYLISTS": remaining})
            self.back("Removed. The Spotify playlist is kept.")
        elif path == "/sync":
            if not is_ready(cfg):
                return self.back("Finish steps 1 and 2 first.", "err")
            start_sync()
            self.back("Syncing now. Refresh in a minute.")
        elif path == "/paste-callback":
            pasted = urlparse(get("url"))
            self.finish_connect(cfg, {k: v[0] for k, v in parse_qs(pasted.query).items()})
        else:
            self.send_error(404)

    def save_playlists(self, cfg, form, get):
        playlists = [p for p in form.get("playlists", []) if p]
        generate = bool(get("GENERATED_ENABLED"))
        days, limit, mode = get("GENERATED_EXCLUDE_DAYS"), get("GENERATED_ARTIST_LIMIT"), get("GENERATED_MODE")
        if not (days.isdigit() and int(days) <= 3650 and limit.isdigit() and int(limit) <= 100 and mode in sync.GENERATED_MODES):
            return self.back("Some Generated Jams settings are invalid.", "err")
        series_options = {}
        for patch in sync.SERIES:
            archive = get(f"arch_{patch}")
            if archive not in [value for value, _ in ARCHIVE_OPTIONS]:
                return self.back("Invalid archive setting.", "err")
            series_options[patch] = {"previous": bool(get(f"prev_{patch}")), "archive_days": int(archive),
                                     "name": get(f"name_{patch}")[:80], "public": bool(get(f"pub_{patch}"))}
        anything = playlists or generate or any(o["previous"] or o["archive_days"] for o in series_options.values()) \
            or any(e.get("enabled", True) for e in cfg["EXTRA_PLAYLISTS"])
        if not anything:
            return self.back("Pick at least one playlist to sync.", "err")
        updates = {
            "SERIES_OPTIONS": series_options,
            "SYNC_PLAYLISTS": ",".join(playlists) or "none",
            "PLAYLIST_PREFIX": get("PLAYLIST_PREFIX")[:40],
            "GENERATED_ENABLED": "true" if generate else "false",
            "GENERATED_NAME": get("GENERATED_NAME")[:80],
            "GENERATED_EXCLUDE_DAYS": days,
            "GENERATED_ARTIST_LIMIT": limit,
            "GENERATED_MODE": mode,
            "SKIP_HATED": "true" if get("SKIP_HATED") else "false",
            "GENERATED_PUBLIC": "true" if get("GENERATED_PUBLIC") else "false",
            "PLAYLIST_PUBLIC": "true" if get("PLAYLIST_PUBLIC") else "false",
        }
        clash = duplicate_name(sync.load_config(updates))
        if clash:
            return self.back(f'Two playlists would both be called "{clash}". Give one of them a different name.', "err")
        save_config(updates)
        self.back("Playlists saved.")

    def add_extra(self, cfg, get):
        playlist_id = sync.parse_playlist_id(get("url"))
        if not playlist_id:
            return self.back("That doesn't look like a ListenBrainz playlist link.", "err")
        if any(e["id"] == playlist_id for e in cfg["EXTRA_PLAYLISTS"]):
            return self.back("That playlist is already on your list.", "err")
        try:
            pl = sync.get_playlist(sync.lb_session(cfg), playlist_id)
        except (sync.SetupError, sync.ServiceError) as e:
            return self.back(str(e), "err")
        except Exception as e:
            return self.back(f"Couldn't reach ListenBrainz: {e}", "err")
        name = get("name")[:80] or pl.get("title") or "ListenBrainz playlist"
        if name in taken_names(cfg):
            return self.back(f'You already have a playlist called "{name}". Pick another name.', "err")
        extras = cfg["EXTRA_PLAYLISTS"] + [{"id": playlist_id, "name": name, "one_time": not get("keep_updated"), "public": bool(get("public"))}]
        save_config({"EXTRA_PLAYLISTS": extras})
        count = len(pl.get("track", []))
        self.back(f'Added "{name}" ({count} songs).')

    def finish_connect(self, cfg, query):
        if query.get("error"):
            return self.back(f"Spotify didn't connect: {query['error']}.", "err")
        created = pending_states.pop(query.get("state", ""), None)
        if not query.get("code") or not created or time.time() - created > 900:
            return self.back("That connection link expired or is incomplete. Click 'Connect Spotify' again.", "err")
        try:
            sync.exchange_code(cfg, query["code"])
            me = sync.Spotify(cfg).me()
        except sync.SetupError as e:
            sync.delete_token()
            return self.back(str(e), "err")
        save_config({"SPOTIFY_CONNECTED_AS": me.get("display_name") or me["id"]})
        if is_ready(sync.load_config()):
            start_sync()
            return self.back("Spotify connected. Your first sync is running now.")
        self.back("Spotify connected.")


def main():
    sync.migrate_secrets()
    cfg = sync.load_config()
    if not install_crontab(cfg["CRON_SCHEDULE"]):
        print(f"Invalid schedule '{cfg['CRON_SCHEDULE']}', falling back to {sync.DEFAULT_SCHEDULE}", flush=True)
        install_crontab(sync.DEFAULT_SCHEDULE)
    if is_ready(cfg) and sync.is_true(os.environ.get("RUN_ON_START", "true")):
        start_sync()
    print(f"Setup page: http://localhost:{PORT}  (schedule: {cfg['CRON_SCHEDULE']})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
