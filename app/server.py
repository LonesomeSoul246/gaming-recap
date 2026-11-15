#!/usr/bin/env python3
"""
Gaming Recap - lokaler Server (Etappe 2).

Quellen   : Playnite-Export + Steam-API (PC), PSN (PS5), OpenXBL (Xbox)
Tracker   : erkennt laufende PC-Spiele und speichert Sessions
HLTB      : Spieldauern von HowLongToBeat
Cloud     : verschlüsselte Kopie auf GitHub Pages fürs Handy
Nur Python-Standardbibliothek. Alles Persönliche bleibt auf diesem PC bzw. verschlüsselt.
"""
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud  # noqa: E402
import extras  # noqa: E402
import hltb  # noqa: E402
import media  # noqa: E402
import notify  # noqa: E402
import steamx  # noqa: E402
import tracker  # noqa: E402
import updater  # noqa: E402
from common import UA, http_json, is_non_game, norm_title, now_iso, parse_iso, read_json, write_json  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

VERSION = "1.0"
BASE = os.path.dirname(os.path.abspath(__file__))
APPDATA = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _pick_data_dir():
    """daten/ neben dem Programm, außer der Ordner ist schreibgeschützt (z. B. unter Programme)."""
    local = os.path.join(BASE, "daten")
    try:
        os.makedirs(local, exist_ok=True)
        probe = os.path.join(local, ".schreibtest")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return local
    except OSError:
        return os.path.join(APPDATA, "GamingRecap", "daten")


DATA_DIR = _pick_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
CACHE_PATH = os.path.join(DATA_DIR, "spiele.json")
XBOX_CACHE_PATH = os.path.join(DATA_DIR, "xbox_cache.json")
CLOUD_STATE_PATH = os.path.join(DATA_DIR, "cloud.json")
STEAMX_PATH = os.path.join(DATA_DIR, "steam_extra.json")
SCORES_PATH = os.path.join(DATA_DIR, "steam_scores.json")
EXTRAS_PATH = os.path.join(DATA_DIR, "extras.json")
MEDIA_PATH = os.path.join(DATA_DIR, "media.json")
HISTORY_DB = os.path.join(DATA_DIR, "verlauf.db")
APP_DB = os.path.join(DATA_DIR, "app.db")
WEB_DIR = os.path.join(BASE, "web")
EXT_SRC = os.path.join(BASE, "playnite-erweiterung", "GamingRecapExport")
PORT = 8765
STATIC = {"/sw.js": "sw.js", "/manifest.webmanifest": "manifest.webmanifest", "/icon-192.png": "icon-192.png",
          "/icon-512.png": "icon-512.png", "/apple-touch-icon.png": "apple-touch-icon.png"}

DEFAULT_CONFIG = {
    "steam_api_key": "", "steam_id": "", "psn_npsso": "", "xbox_api_key": "",
    "playnite_export": os.path.join(APPDATA, "GamingRecap", "playnite.json"),
    "sync_hours": 6, "lan": False, "hltb": True, "tracker": True,
    "cloud_enabled": False, "gh_repo": "", "gh_token": "", "cloud_pass": "",
    "steam_extras": True, "notify_sales": True, "notify_weekly": True, "pause_hours": 0,
    "watts": 350, "kwh_price": 0.35, "igdb_client_id": "", "igdb_client_secret": "",
    "backlog_stores": ["Steam", "Ubisoft", "EA", "GOG", "Epic Games"],
    "push_topic": "", "notify_free": True, "notify_release": True, "itad_key": "", "sgdb_key": "", "rawg_key": "", "psn_set_at": 0,
    "publish_updates": False, "update_url": "", "auto_update": True, "user_name": "", "prices_steam_only": True,
    "partner_url": "", "partner_code": "",
}
SECRET_FIELDS = ("steam_api_key", "psn_npsso", "xbox_api_key", "gh_token", "cloud_pass", "igdb_client_secret", "itad_key", "sgdb_key", "rawg_key", "partner_code")
TIERS = {"S", "A", "B", "C", "D", "F"}
STATUSES = {"ungespielt", "als_naechstes", "angefangen", "pausiert", "durchgespielt",
            "komplett", "abgebrochen", "endlos", "ignoriert"}


# ================================================================ Hilfen

def ts_to_iso(ts):
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds") if ts > 0 else None


def as_list(v):
    if v is None:
        return []
    return [x for x in v if x is not None] if isinstance(v, list) else [v]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in read_json(CONFIG_PATH, {}).items() if k in DEFAULT_CONFIG})
    return cfg


def err_text(e):
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        return "Keine Verbindung zum Server der Plattform."
    return str(e) or e.__class__.__name__


def steam_images(appid):
    base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"
    return [f"{base}/library_600x900.jpg"], [f"{base}/library_hero.jpg", f"{base}/header.jpg"]


# ================================================================ Steam

def resolve_steam_id(key, raw):
    raw = (raw or "").strip()
    m = re.search(r"(\d{17})", raw)
    if m:
        return m.group(1)
    m = re.search(r"/id/([^/?#]+)", raw)
    name = m.group(1) if m else raw.strip("/")
    if not name:
        raise RuntimeError("Keine Steam-ID oder kein Profil-Link eingetragen.")
    r = http_json("https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?"
                  + urllib.parse.urlencode({"key": key, "vanityurl": name})).get("response", {})
    if r.get("success") == 1:
        return r["steamid"]
    raise RuntimeError("Steam-Profil nicht gefunden. Prüfe Steam-ID bzw. Profil-Link.")


def fetch_steam(cfg):
    key = cfg["steam_api_key"].strip()
    try:
        sid = resolve_steam_id(key, cfg["steam_id"])
        j = http_json("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?"
                      + urllib.parse.urlencode({"key": key, "steamid": sid, "include_appinfo": 1,
                                                "include_played_free_games": 1, "format": "json"}))
    except urllib.error.HTTPError as e:
        raise RuntimeError("Steam lehnt den API-Key ab. Prüfe den Key." if e.code in (401, 403) else f"Steam-Fehler {e.code}")
    games = j.get("response", {}).get("games")
    if games is None:
        raise RuntimeError("Steam liefert keine Spiele. Stelle in Steam unter Profil > Privatsphäre "
                           "die 'Spieldetails' auf 'Öffentlich'.")
    out = {}
    for g in games:
        appid = str(g.get("appid"))
        covers, banners = steam_images(appid)
        out[appid] = {"id": f"steam_{appid}", "title": g.get("name") or f"Steam-App {appid}", "store": "Steam",
                      "device": "PC", "minutes": int(g.get("playtime_forever") or 0),
                      "last_played": ts_to_iso(g.get("rtime_last_played")), "steam_appid": appid,
                      "two_weeks": int(g.get("playtime_2weeks") or 0), "covers": covers, "banners": banners}
    return out


# ================================================================ Playnite

STORE_RULES = [("steam", "Steam"), ("epic", "Epic Games"), ("ubisoft", "Ubisoft"), ("uplay", "Ubisoft"),
               ("ea app", "EA"), ("origin", "EA"), ("electronic arts", "EA"), ("gog", "GOG"),
               ("xbox", "Xbox PC"), ("microsoft", "Xbox PC"), ("battle", "Battle.net"),
               ("amazon", "Amazon"), ("itch", "itch.io"), ("humble", "Humble"), ("rockstar", "Rockstar")]
PN_IMAGES = {}


def store_name(source):
    s = (source or "").lower()
    return next((v for k, v in STORE_RULES if k in s), source or "Andere")


def is_pc(platforms):
    plats = [str(p).lower() for p in as_list(platforms)]
    return not plats or any(x in p for p in plats for x in ("pc", "windows", "linux", "mac"))


def console_device(platforms):
    """Konsole aus Playnite-Plattformen ableiten (nur für Spiele ohne PC-Plattform)."""
    plats = " ".join(str(p).lower() for p in as_list(platforms))
    if "xbox" in plats:
        return "Xbox"
    if "playstation" in plats or re.search(r"\bps[1-5]\b", plats):
        return "PlayStation"
    if "nintendo" in plats or "switch" in plats:
        return "Nintendo"
    return None


def load_playnite(cfg):
    path = cfg["playnite_export"]
    if not os.path.exists(path):
        raise RuntimeError("Noch kein Playnite-Export gefunden. Installiere die Playnite-Erweiterung "
                           "(Einstellungen) und starte Playnite einmal neu.")
    doc = read_json(path, None)
    if not isinstance(doc, dict):
        raise RuntimeError("Playnite-Export ist beschädigt. Starte Playnite neu.")
    games = doc.get("games") or []
    return ([games] if isinstance(games, dict) else games), doc.get("exported")


def autostart_file():
    ad = os.environ.get("APPDATA")
    if os.name != "nt" or not ad:
        return None
    return os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "GamingRecap.cmd")


def set_autostart(on):
    f = autostart_file()
    if not f:
        raise RuntimeError("Autostart gibt es nur unter Windows.")
    if on:
        bat = os.path.join(BASE, "Start.bat")
        with open(f, "w", encoding="ascii", errors="ignore", newline="\r\n") as h:
            h.write('@echo off\nstart "Gaming Recap" /min "%s"\n' % bat)
    elif os.path.exists(f):
        os.remove(f)
    return bool(f and os.path.exists(f))


def playnite_has_console(cfg, dev):
    try:
        games, _ = load_playnite(cfg)
    except Exception:
        return False
    return any(not is_pc(p.get("platforms")) and console_device(p.get("platforms")) == dev for p in games)


def pn_record(p):
    pid = p.get("id")
    rec = {"id": f"pn_{pid}", "title": p.get("name") or "Unbekannt", "store": store_name(p.get("source")),
           "device": "PC", "minutes": int(p.get("playtime") or 0) // 60, "last_played": p.get("lastActivity"),
           "covers": [], "banners": []}
    if "family" in str(p.get("source") or "").lower():
        rec["family"] = True  # über Steam Family Sharing geliehen: zählt in Statistik, nicht in den Backlog
    if rec["store"] == "Steam" and str(p.get("gameId") or "").isdigit():
        rec["covers"], rec["banners"] = steam_images(p["gameId"])
        rec["steam_appid"] = str(p["gameId"])
    return rec


def enrich_from_playnite(rec, p):
    rec.update({
        "playnite_id": p.get("id"), "genres": [str(x) for x in as_list(p.get("genres"))],
        "features": [str(x) for x in as_list(p.get("features"))],
        "developers": [str(x) for x in as_list(p.get("developers"))],
        "release_year": p.get("releaseYear"), "added": p.get("added"), "completion": p.get("completion"),
        "installed": bool(p.get("installed")), "hidden": bool(p.get("hidden")), "favorite": bool(p.get("favorite")),
        "critic": p.get("criticScore"), "community": p.get("communityScore"), "play_count": p.get("playCount"),
        "install_dir": p.get("installDir"),
    })
    if not rec.get("last_played") and p.get("lastActivity"):
        rec["last_played"] = p["lastActivity"]
    pid = p.get("id")
    if pid and (p.get("cover") or p.get("background")):
        PN_IMAGES[pid] = {"cover": p.get("cover"), "background": p.get("background")}
        # Playnite-Cover zuerst: das sind die Bilder, die du auch in Playnite siehst
        if p.get("cover"):
            rec["covers"] = [f"/api/img/{pid}/cover"] + [c for c in rec["covers"] if not c.startswith("/api/img")]
        if p.get("background"):
            rec["banners"] = [f"/api/img/{pid}/background"] + [c for c in rec["banners"] if not c.startswith("/api/img")]


def build_pc(cfg, sources):
    steam = None
    if cfg["steam_api_key"].strip() and cfg["steam_id"].strip():
        try:
            steam = fetch_steam(cfg)
            sources["Steam"] = {"ok": True, "count": len(steam), "minutes": sum(g["minutes"] for g in steam.values())}
        except Exception as e:
            sources["Steam"] = {"ok": False, "error": err_text(e)}
    pn_games, pn_console = [], []
    try:
        pn_all, exported = load_playnite(cfg)
        pn_games = [p for p in pn_all if is_pc(p.get("platforms"))]
        # Konsolenspiele aus Playnite (z. B. Xbox-Konto) - außer die Konsole kommt schon direkt
        direct = set(cfg.get("_direct_devices") or [])
        for p in pn_all:
            dev = None if is_pc(p.get("platforms")) else console_device(p.get("platforms"))
            if dev and dev not in direct and not is_non_game(p.get("name")):
                pn_console.append((p, dev))
        pn_games = [p for p in pn_games if not is_non_game(p.get("name"))]
        sources["Playnite"] = {"ok": True, "count": len(pn_games) + len(pn_console),
                               "minutes": sum(int(p.get("playtime") or 0) // 60 for p in pn_games)
                               + sum(int(p.get("playtime") or 0) // 60 for p, _ in pn_console),
                               "info": (f"{len(pn_games)} PC, {len(pn_console)} Konsole · " if pn_console else "")
                               + (f"Export vom {exported[:10]}" if exported else "")}
    except Exception as e:
        sources["Playnite"] = {"ok": False, "error": err_text(e)}

    PN_IMAGES.clear()
    out = {f"steam_{k}": v for k, v in (steam or {}).items()}
    for p in pn_games:
        gid = str(p.get("gameId") or "")
        key = f"steam_{gid}" if store_name(p.get("source")) == "Steam" and gid.isdigit() else None
        if key and key in out:  # Steam-API liefert die Spielzeit, Playnite die Metadaten
            enrich_from_playnite(out[key], p)
            continue
        rec = pn_record(p)
        enrich_from_playnite(rec, p)
        rec["id"] = key or rec["id"]
        installed = bool(p.get("installed"))
        # Ubisoft Connect listet alles vom Ubisoft-Konto, auch Käufe auf PS/Xbox:
        # nur installierte oder am PC (über Playnite) gespielte Spiele zählen als PC-Spiel.
        if rec["store"] == "Ubisoft" and not installed and not rec["minutes"]:
            continue
        # Xbox-Konto: Spielzeit gilt fürs ganze Konto, meist auf der Konsole gespielt; Game Pass von früher.
        # Nur installierte Spiele zählen als PC-Spiel, der Rest als Xbox.
        if rec["store"] == "Xbox PC" and not installed:
            rec.update(device="Xbox", store="Xbox", xbox_account=True)
        out[rec["id"]] = rec
    for p, dev in pn_console:
        rec = pn_record(p)
        enrich_from_playnite(rec, p)
        rec.update(id=f"pnc_{p.get('id')}", device=dev, store=dev)
        out[rec["id"]] = rec
    # Gleiches Spiel mehrfach vom Xbox-Konto (PC- und Konsolen-Eintrag): nur einmal zählen
    best = {}
    for rid, r in list(out.items()):
        if r.get("device") == "Xbox":
            k = norm_title(r["title"])
            if k in best:
                other = out[best[k]]
                drop = rid if (r.get("minutes") or 0) <= (other.get("minutes") or 0) else best[k]
                keep = best[k] if drop == rid else rid
                out[keep]["installed"] = out[keep].get("installed") or out[drop].get("installed")
                del out[drop]
                best[k] = keep
            else:
                best[k] = rid
    return list(out.values())


# ================================================================ PlayStation

PSN_CLIENT = "09515159-7237-4370-9b40-3806e67c0891"
PSN_BASIC = "MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
PSN_REDIRECT = "com.scee.psxandroid.scecompcall://redirect"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def psn_access_token(npsso):
    q = urllib.parse.urlencode({"access_type": "offline", "client_id": PSN_CLIENT, "redirect_uri": PSN_REDIRECT,
                                "response_type": "code", "scope": "psn:mobile.v2.core psn:clientapp"})
    req = urllib.request.Request("https://ca.account.sony.com/api/authz/v3/oauth/authorize?" + q,
                                 headers={"Cookie": f"npsso={npsso.strip()}", "User-Agent": UA})
    location = None
    try:
        location = urllib.request.build_opener(_NoRedirect).open(req, timeout=30).headers.get("Location")
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location")
    if not location or "code=" not in location:
        raise RuntimeError("PlayStation-Anmeldung fehlgeschlagen. Der NPSSO-Token ist vermutlich abgelaufen. "
                           "Hol dir einen neuen (siehe Einstellungen).")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]
    body = urllib.parse.urlencode({"code": code, "redirect_uri": PSN_REDIRECT,
                                   "grant_type": "authorization_code", "token_format": "jwt"}).encode()
    return http_json("https://ca.account.sony.com/api/authz/v3/oauth/token", data=body, method="POST",
                     headers={"Authorization": "Basic " + PSN_BASIC,
                              "Content-Type": "application/x-www-form-urlencoded"})["access_token"]


def iso_duration_minutes(s):
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$", s or "")
    if not m:
        return 0
    d, h, mi, se = m.groups()
    return int(d or 0) * 1440 + int(h or 0) * 60 + int(mi or 0) + round(float(se or 0) / 60)


def fetch_psn(cfg, skipped=None):
    token = psn_access_token(cfg["psn_npsso"])
    out, offset = [], 0
    skipped = skipped if skipped is not None else []
    seen_cats = {}
    fetch_psn.last_categories = seen_cats
    while True:
        j = http_json("https://m.np.playstation.com/api/gamelist/v2/users/me/titles?"
                      + urllib.parse.urlencode({"limit": 200, "offset": offset}),
                      headers={"Authorization": "Bearer " + token})
        for t in j.get("titles", []):
            img = t.get("imageUrl")
            if is_non_game(t.get("name"), t.get("category")):
                skipped.append(f"{t.get('name')} [{t.get('category')}]")
                continue
            seen_cats[str(t.get("category"))] = seen_cats.get(str(t.get("category")), 0) + 1
            out.append({"id": f"psn_{t.get('titleId')}", "title": t.get("name") or t.get("titleId"),
                        "store": "PlayStation", "device": "PlayStation",
                        "minutes": iso_duration_minutes(t.get("playDuration")),
                        "last_played": t.get("lastPlayedDateTime"), "first_played": t.get("firstPlayedDateTime"),
                        "play_count": t.get("playCount"),
                        "covers": [img] if img else [], "banners": [img] if img else []})
        nxt = j.get("nextOffset")
        if not nxt or nxt <= offset or not j.get("titles"):
            break
        offset = nxt
    return out


# ================================================================ Xbox (OpenXBL)

XBOX_CONSOLES = {"xboxseries", "xboxone", "xbox360"}


def _find_minutes(obj):
    if isinstance(obj, dict):
        if str(obj.get("name", "")).lower() == "minutesplayed" and obj.get("value") not in (None, ""):
            try:
                return int(float(obj["value"]))
            except (TypeError, ValueError):
                pass
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            r = _find_minutes(v)
            if r is not None:
                return r
    return None


def fetch_xbox(cfg, max_calls=80):
    H = {"X-Authorization": cfg["xbox_api_key"].strip(), "Accept-Language": "de-DE"}
    try:
        acc = http_json("https://xbl.io/api/v2/account", headers=H)
    except urllib.error.HTTPError as e:
        raise RuntimeError("OpenXBL lehnt den API-Key ab. Prüfe den Key auf xbl.io." if e.code in (401, 403)
                           else f"OpenXBL-Fehler {e.code}")
    xuid = str(((acc.get("profileUsers") or [{}])[0]).get("id") or "")
    titles = None
    for path in ("/api/v2/player/titleHistory", f"/api/v2/player/titleHistory/{xuid}"):
        try:
            titles = http_json("https://xbl.io" + path, headers=H).get("titles")
            if titles is not None:
                break
        except urllib.error.HTTPError:
            continue
    if titles is None:
        raise RuntimeError("Xbox-Spieleliste konnte nicht geladen werden.")
    cache = read_json(XBOX_CACHE_PATH, {})
    calls, out, missing = 0, [], 0
    for t in titles:
        devices = {str(d).lower() for d in as_list(t.get("devices"))}
        if t.get("type", "Game") != "Game" or not devices & XBOX_CONSOLES:
            continue
        tid = str(t.get("titleId"))
        last = (t.get("titleHistory") or {}).get("lastTimePlayed")
        c = cache.get(tid)
        mins = c["minutes"] if c and c.get("last") == last else None
        if mins is None and calls < max_calls:
            calls += 1
            try:
                mins = _find_minutes(http_json(f"https://xbl.io/api/v2/achievements/stats/{tid}", headers=H))
            except Exception:
                mins = None
            if mins is not None:
                cache[tid] = {"last": last, "minutes": mins}
        if mins is None:
            mins = c["minutes"] if c else 0
            missing += 1
        img = t.get("displayImage")
        out.append({"id": f"xbox_{tid}", "title": t.get("name") or tid, "store": "Xbox", "device": "Xbox",
                    "minutes": int(mins or 0), "last_played": last,
                    "covers": [img] if img else [], "banners": [img] if img else []})
    write_json(XBOX_CACHE_PATH, cache)
    return out, missing


# ================================================================ Datenbanken

def history_con():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(HISTORY_DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS snapshots (day TEXT, id TEXT, platform TEXT, minutes INTEGER, "
                "PRIMARY KEY (day, id))")
    return con


def app_con():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(APP_DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, status TEXT, rating INTEGER, "
                "note TEXT, updated TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS status_log (key TEXT, status TEXT, at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
    cols = [r[1] for r in con.execute("PRAGMA table_info(meta)")]
    if "tier" not in cols:
        con.execute("ALTER TABLE meta ADD COLUMN tier TEXT")
    if "hide" not in cols:
        con.execute("ALTER TABLE meta ADD COLUMN hide INTEGER")
    tracker.init_db(con)
    hltb.init_db(con)
    return con


def record_snapshot(games):
    day = dt.date.today().isoformat()
    con = history_con()
    with con:
        con.executemany("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?)",
                        [(day, g["id"], g["device"], g["minutes"]) for g in games])
    con.close()


# ---------------------------------------------------------------- Konsolen-Sessions
# Konsolen liefern nur Gesamtzeit + "zuletzt gespielt". Wir vergleichen jeden Abgleich mit dem vorigen:
# Zuwachs = gespielt zwischen den beiden Abgleichen. Liegt nur ein Tag dazwischen, ist der Tag sicher;
# sonst landet ein Abend-Block auf dem letzten Spieltag und der Rest verteilt sich (markiert als "ungefähr").

def console_con():
    con = app_con()
    con.execute("CREATE TABLE IF NOT EXISTS console_sessions (game_key TEXT, device TEXT, start INTEGER, "
                "seconds INTEGER, approx INTEGER, PRIMARY KEY (game_key, device, start))")
    return con


def record_console_sessions(prev_games, prev_sync, games):
    prev = {g["id"]: g for g in prev_games or []}
    t_prev = parse_iso(prev_sync) if prev_sync else None
    if not t_prev:
        return 0
    now = time.time()
    rows = []
    for g in games:
        if g.get("device") == "PC":
            continue
        p = prev.get(g["id"])
        if not p or p.get("device") != g.get("device"):
            continue
        delta = int(g.get("minutes") or 0) - int(p.get("minutes") or 0)
        if delta <= 0 or delta > 24 * 60 * max(1, (now - t_prev) / 86400 + 1):
            continue
        last = parse_iso(g.get("last_played")) or now
        last = min(max(last, t_prev), now)
        key = norm_title(g["title"])
        secs = delta * 60
        d_last = dt.date.fromtimestamp(last)
        d_first = dt.date.fromtimestamp(t_prev)
        if d_first == d_last:
            rows.append((key, g["device"], int(last - secs), secs, 0))
            continue
        # mehrere Tage: Block am letzten Spieltag (max. 4 Std), Rest gleichmäßig auf die Tage davor
        midnight = dt.datetime.combine(d_last, dt.time(0, 0)).timestamp()
        block = int(min(secs, 4 * 3600, max(1800, last - midnight)))
        rows.append((key, g["device"], int(last - block), block, 1))
        rest = secs - block
        days = []
        d = d_last - dt.timedelta(days=1)
        while d >= d_first and len(days) < 60:
            days.append(d)
            d -= dt.timedelta(days=1)
        each = rest // len(days) if days else 0
        for d in days:
            if each > 0:
                rows.append((key, g["device"], int(dt.datetime.combine(d, dt.time(20, 0)).timestamp()), int(each), 1))
    if rows:
        con = console_con()
        with con:
            con.executemany("INSERT OR REPLACE INTO console_sessions VALUES (?, ?, ?, ?, ?)", rows)
        con.close()
    return len(rows)


def load_console_sessions(since):
    con = console_con()
    rows = con.execute("SELECT game_key, start, seconds, device, approx FROM console_sessions WHERE start >= ? "
                       "ORDER BY start", (since,)).fetchall()
    con.close()
    return [list(r) for r in rows]


def period_minutes(games, period):
    today = dt.date.today()
    start = {"year": today.replace(month=1, day=1), "month": today.replace(day=1),
             "week": today - dt.timedelta(days=7)}.get(period)
    if start is None:
        return None, {"period": "all"}
    con = history_con()
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
    info = {"period": period, "start": start.isoformat(), "tracking_since": days[0] if days else None}
    if not days:
        con.close()
        return {}, info
    # Ausgangswert = Stand am Ende des Vortags (jeder Tag speichert seinen letzten Stand)
    before = [d for d in days if d < start.isoformat()]
    base_day = before[-1] if before else days[0]
    info.update(base_day=base_day, complete=bool(before))
    baseline = dict(con.execute("SELECT id, minutes FROM snapshots WHERE day = ?", (base_day,)).fetchall())
    base_devices = {r[0] for r in con.execute("SELECT DISTINCT platform FROM snapshots WHERE day = ?", (base_day,))}
    out = {}
    for g in games:
        if g["id"] in baseline:
            b = baseline[g["id"]]
        elif g["device"] in base_devices:
            b = 0
        else:
            row = con.execute("SELECT minutes FROM snapshots WHERE id = ? ORDER BY day LIMIT 1", (g["id"],)).fetchone()
            b = row[0] if row else g["minutes"]
        if g["minutes"] - b > 0:
            out[g["id"]] = g["minutes"] - b
    con.close()
    return out, info


def load_meta():
    con = app_con()
    rows = con.execute("SELECT key, status, rating, note, updated, tier, hide FROM meta").fetchall()
    con.close()
    return {k: {"status": s, "rating": r, "note": n, "updated": u, "tier": t, "hide": bool(h)} for k, s, r, n, u, t, h in rows}


def save_meta(body):
    key = norm_title(body.get("key"))
    if not key:
        raise ValueError("key fehlt")
    con = app_con()
    row = con.execute("SELECT status, rating, note, tier, hide FROM meta WHERE key = ?", (key,)).fetchone()
    status, rating, note, tier, hide = row if row else (None, None, None, None, None)
    if "status" in body:
        s = body["status"]
        if s is not None and s not in STATUSES:
            con.close()
            raise ValueError("Unbekannter Status")
        if s != status:
            con.execute("INSERT INTO status_log VALUES (?, ?, ?)", (key, s, now_iso()))
        status = s
    if "rating" in body:
        r = body["rating"]
        rating = int(r) if isinstance(r, (int, float)) and 0 < r <= 5 else None
    if "note" in body:
        note = str(body["note"] or "")[:2000] or None
    if "tier" in body:
        tier = body["tier"] if body["tier"] in TIERS else None
    if "hide" in body:
        hide = 1 if body["hide"] else None
    updated = now_iso()
    with con:
        con.execute("INSERT OR REPLACE INTO meta (key, status, rating, note, updated, tier, hide) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, status, rating, note, updated, tier, hide))
    con.close()
    cloud_state["pending"] = True
    return {"key": key, "status": status, "rating": rating, "note": note, "updated": updated, "tier": tier, "hide": bool(hide)}


KV_KEYS = {"goals", "year_overrides", "wrapped", "notpc", "snotes", "covers", "paid", "notify", "critic"}


def kv_get(key, default=None):
    con = app_con()
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    con.close()
    try:
        return json.loads(row[0]) if row else default
    except ValueError:
        return default


def kv_set(key, value):
    con = app_con()
    with con:
        con.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (key, json.dumps(value, ensure_ascii=False), now_iso()))
    con.close()


def kv_updated(key):
    con = app_con()
    row = con.execute("SELECT updated FROM kv WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else None


def clean_goals(v):
    out = {}
    if isinstance(v, dict):
        for year, g in list(v.items())[:10]:
            if re.fullmatch(r"\d{4}", str(year)) and isinstance(g, dict):
                out[str(year)] = {k: int(g[k]) for k in ("done", "max_new", "hours") if isinstance(g.get(k), (int, float)) and 0 < g[k] < 100000}
    return out


NOTIFY_CATS = ("sales", "lows", "free", "release", "weekly", "monthly", "yearly", "pause", "health", "offline")


def notify_prefs(cfg=None):
    """Benachrichtigungs-Einstellungen (am PC und am Handy änderbar). Fehlen sie, gelten die alten Schalter."""
    v = kv_get("notify", None)
    if isinstance(v, dict) and v.get("cats"):
        return v
    cfg = cfg or load_config()
    return {"cats": {"sales": bool(cfg.get("notify_sales", True)), "lows": bool(cfg.get("notify_sales", True)),
                     "free": bool(cfg.get("notify_free", True)), "release": bool(cfg.get("notify_release", True)),
                     "weekly": bool(cfg.get("notify_weekly", True)), "monthly": bool(cfg.get("notify_weekly", True)),
                     "yearly": True, "pause": True, "health": True, "offline": True},
            "win": True, "phone": True, "quiet": None, "pause_h": float(cfg.get("pause_hours") or 0)}


def in_quiet(prefs, now=None):
    q = prefs.get("quiet")
    if not q:
        return False
    h = (now or dt.datetime.now()).hour
    a, b = q
    return a <= h < b if a < b else (h >= a or h < b)


def alert(kind, title, body, url=None):
    """Zentrale Stelle für alle Hinweise: Thema an/aus, Windows und/oder Handy, Nachtruhe fürs Handy."""
    prefs = notify_prefs()
    if not (prefs.get("cats") or {}).get(kind, True):
        return False
    phone = bool(prefs.get("phone", True))
    if phone and in_quiet(prefs):
        q = kv_get("push_queue", []) or []
        q.append([title, body, url])
        kv_set("push_queue", q[-15:])
        phone = False
    notify.toast(title, body, url, phone=phone, win=bool(prefs.get("win", True)))
    return True


def flush_push_queue():
    """Nach der Nachtruhe: aufgeschobene Push-Nachrichten nachreichen."""
    prefs = notify_prefs()
    q = kv_get("push_queue", []) or []
    if not q or in_quiet(prefs):
        return
    kv_set("push_queue", [])
    if not prefs.get("phone", True):
        return
    if len(q) > 3:
        notify.push(f"Gaming Recap: {len(q)} Hinweise aus der Nacht", " · ".join(x[0] for x in q)[:300])
    else:
        for t, b, u in q:
            notify.push(t, b, u)


def clean_kv(key, v):
    if key == "goals":
        return clean_goals(v)
    if key == "snotes":  # Session-Notizen: {"key|start": [stimmung 1-3, text]}
        if not isinstance(v, dict):
            return {}
        return {str(k)[:140]: [int(x[0]) if str(x[0]).isdigit() else 0, str(x[1] or "")[:280]]
                for k, x in list(v.items())[-2000:] if isinstance(x, list) and len(x) == 2}
    if key == "notify":  # Benachrichtigungen: Themen, Kanäle, Nachtruhe
        if not isinstance(v, dict):
            return {}
        out = {"cats": {k: bool((v.get("cats") or {}).get(k, True)) for k in NOTIFY_CATS},
               "win": bool(v.get("win", True)), "phone": bool(v.get("phone", True))}
        q = v.get("quiet")
        if isinstance(q, list) and len(q) == 2 and all(isinstance(x, int) and 0 <= x <= 23 for x in q) and q[0] != q[1]:
            out["quiet"] = q
        ph = v.get("pause_h")
        out["pause_h"] = float(ph) if isinstance(ph, (int, float)) and 0 <= ph <= 24 else 0
        return out
    if key == "critic":  # selbst eingetragene Wertung: {spielschlüssel: 1-100}
        if not isinstance(v, dict):
            return {}
        return {str(k)[:120]: int(x) for k, x in list(v.items())[:3000] if isinstance(x, (int, float)) and 1 <= x <= 100}
    if key == "paid":  # selbst bezahlter Preis: {spielschlüssel: cent | -1 geschenkt | -2 Abo}
        if not isinstance(v, dict):
            return {}
        return {str(k)[:120]: int(x) for k, x in list(v.items())[:5000] if isinstance(x, (int, float)) and -2 <= x < 10 ** 6}
    if key == "covers":  # selbst gewählte Cover: {spielschlüssel: https-URL}
        if not isinstance(v, dict):
            return {}
        return {str(k)[:120]: str(u)[:500] for k, u in list(v.items())[:2000]
                if isinstance(u, str) and re.fullmatch(r"https://[^\s\"'<>]+", u)}
    if key == "notpc":
        return {str(k)[:120]: 1 for k, x in list((v or {}).items())[:2000] if x} if isinstance(v, dict) else {}
    out = {}
    if not isinstance(v, dict):
        return out
    for year, d in sorted(v.items())[-60:]:  # die neuesten behalten (Jahre bleiben, Monate kürzt die App selbst)
        if not re.fullmatch(r"\d{4}(-\d{2})?", str(year)) or not isinstance(d, dict):
            continue
        if key == "year_overrides":
            out[year] = {str(k)[:120]: int(m) for k, m in list(d.items())[:500] if isinstance(m, (int, float)) and 0 <= m < 10 ** 6}
        else:  # wrapped-Archiv: {key: [minuten, {gerät: minuten}]}
            games = d.get("games") if isinstance(d.get("games"), dict) else {}
            out[year] = {"saved": str(d.get("saved", ""))[:40], "estimated": bool(d.get("estimated")), "final": bool(d.get("final")),
                         "games": {str(k)[:120]: [int(x[0]), {str(a)[:20]: int(b) for a, b in (x[1] or {}).items()}]
                                   for k, x in list(games.items())[:3000]
                                   if isinstance(x, list) and len(x) == 2 and isinstance(x[0], (int, float))}}
    return out


def load_status_log(days=800):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    con = app_con()
    rows = con.execute("SELECT key, status, at FROM status_log WHERE at >= ? ORDER BY at", (since,)).fetchall()
    con.close()
    return [list(r) for r in rows]


def apply_remote_edits(payload):
    """Änderungen vom Handy übernehmen, sofern sie neuer sind als der Stand am PC."""
    meta = load_meta()
    n = 0
    for e in payload.get("edits", []) if isinstance(payload, dict) else []:
        if not isinstance(e, dict) or not e.get("key"):
            continue
        if e.get("kind") == "kv":
            k = e.get("key")
            upd = kv_updated(k)
            if k in KV_KEYS and k != "wrapped" and not (upd and (parse_iso(upd) or 0) >= (e.get("ts") or 0) / 1000):
                kv_set(k, clean_kv(k, e.get("value")))
                n += 1
            continue
        cur = meta.get(norm_title(e["key"]))
        ts = (e.get("ts") or 0) / 1000
        if cur and cur.get("updated") and (parse_iso(cur["updated"]) or 0) >= ts:
            continue
        patch = {k: e[k] for k in ("status", "rating", "note", "tier", "hide") if k in e}
        try:
            save_meta(dict(patch, key=e["key"]))
            n += 1
        except ValueError:
            pass
    return n


# ================================================================ Synchronisierung

state = {"syncing": False}
state_lock = threading.Lock()
hltb_state = {"running": False, "error": None, "done": 0, "left": 0}
steamx_state = {"running": False, "error": None}
notified = {"pause": set()}
cloud_state = read_json(CLOUD_STATE_PATH, {}) if os.path.exists(CLOUD_STATE_PATH) else {}
cloud_lock = threading.Lock()
TRACKER = tracker.Tracker(APP_DB)


def load_cache():
    return read_json(CACHE_PATH, {"last_sync": None, "sources": {}, "games": []})


def tracker_targets(games):
    sd = tracker.steam_install_dirs()
    items = []
    for g in games:
        if g.get("device") != "PC":
            continue
        key = norm_title(g["title"])
        if g.get("install_dir"):
            items.append((g["install_dir"], key, g["title"]))
        if g.get("steam_appid") in sd:
            items.append((sd[g["steam_appid"]], key, g["title"]))
    return items


def run_sync():
    with state_lock:
        if state["syncing"]:
            return
        state["syncing"] = True
    print(time.strftime("%H:%M"), "Synchronisiere ...")
    try:
        cfg = load_config()
        sources = {}
        # Konsolen direkt nur dort, wo die direkte Quelle auch geklappt hat
        direct = set()
        psn_games, xbox_games = [], []
        if cfg["psn_npsso"].strip():
            try:
                skipped = []
                g = fetch_psn(cfg, skipped)
                psn_games = g
                direct.add("PlayStation")
                sources["PlayStation"] = {"ok": True, "count": len(g), "minutes": sum(x["minutes"] for x in g),
                                          "info": f"{len(skipped)} Apps ausgeblendet: {', '.join(sorted(set(n.split(' [')[0] for n in skipped))[:8])}" if skipped else None}
            except Exception as e:
                sources["PlayStation"] = {"ok": False, "error": err_text(e)}
        pn_has_xbox = playnite_has_console(cfg, "Xbox")
        if cfg["xbox_api_key"].strip() and pn_has_xbox:
            sources["Xbox"] = {"ok": True, "count": None, "minutes": None,
                               "info": "kommt über Playnite – OpenXBL-Key wird nicht gebraucht"}
        elif cfg["xbox_api_key"].strip():
            try:
                g, missing = fetch_xbox(cfg)
                xbox_games = g
                direct.add("Xbox")
                sources["Xbox"] = {"ok": True, "count": len(g), "minutes": sum(x["minutes"] for x in g),
                                   "info": f"{missing} Spielzeiten folgen beim nächsten Abgleich" if missing else None}
            except Exception as e:
                sources["Xbox"] = {"ok": False, "error": err_text(e)}
        games = build_pc(dict(cfg, _direct_devices=sorted(direct)), sources) + psn_games + xbox_games
        for name, s in sources.items():
            print(f"  {name}: " + ((f"{s['count']} Spiele" if s.get("count") is not None else s.get("info") or "ok") if s["ok"] else "FEHLER - " + s["error"]))
        old = load_cache()
        try:
            n = record_console_sessions(old.get("games"), old.get("last_sync"), games)
            if n:
                print(f"  {n} Konsolen-Sessions ergänzt")
        except Exception as e:
            print("  Konsolen-Sessions:", e)
        write_json(CACHE_PATH, {"last_sync": now_iso(), "sources": sources, "games": games, "pn_images": PN_IMAGES})
        if games:
            record_snapshot(games)
            try:
                if finalize_archives(games):
                    print("  Wrapped-Archiv abgeschlossen")
            except Exception as e:
                print("  Wrapped-Archiv:", e)
        TRACKER.set_targets(tracker_targets(games))
        try:
            idmap = {g["playnite_id"]: norm_title(g["title"]) for g in games if g.get("playnite_id")}
            n = tracker.import_playnite_sessions(os.path.join(os.path.dirname(cfg["playnite_export"]), "sessions.jsonl"), APP_DB, idmap)
            if n:
                print(f"  {n} Playnite-Sessions übernommen")
        except Exception as e:
            print("  Playnite-Sessions:", e)
        cloud_state["pending"] = True
        if cfg.get("hltb"):
            threading.Thread(target=hltb_worker, args=(games,), daemon=True).start()
        if cfg.get("steam_extras") and cfg["steam_api_key"].strip() and sources.get("Steam", {}).get("ok"):
            threading.Thread(target=steamx_worker, args=(cfg, games), daemon=True).start()
        threading.Thread(target=scores_worker, args=(games,), daemon=True).start()
        threading.Thread(target=extras_worker, args=(cfg,), daemon=True).start()
        threading.Thread(target=media_worker, args=(cfg, games), daemon=True).start()
    except Exception:
        traceback.print_exc()
    finally:
        state["syncing"] = False


def hltb_worker(games):
    if hltb_state["running"]:
        return
    hltb_state.update(running=True, done=0, total=0)
    try:
        wanted = {}
        for g in sorted(games, key=lambda g: g["device"] != "PC"):  # PC-Spiele zuerst
            if g["device"] == "PC" and g.get("hidden"):
                continue
            k = norm_title(g["title"])
            if k not in wanted or (g.get("steam_appid") and not wanted[k][1]):
                wanted[k] = (g["title"], g.get("steam_appid"))
        # Wunschliste dazu (für den Kauf-Check: Preis pro Spielstunde)
        sx = read_json(STEAMX_PATH, {})
        for w in sx.get("wishlist") or []:
            nm = ((sx.get("prices") or {}).get(str(w.get("appid"))) or {}).get("name")
            if nm:
                wanted.setdefault(norm_title(nm), (nm, w.get("appid")))
        cfg = load_config()
        cid, secret = cfg.get("igdb_client_id", "").strip(), cfg.get("igdb_client_secret", "").strip()
        if not (cid and secret):
            hltb_state.update(error=None, left=0, missing=True)
            return
        hltb_state["missing"] = False
        new, errors, left = hltb.update(APP_DB, [(k, t, a) for k, (t, a) in wanted.items()], cid, secret, status=hltb_state)
        hltb_state.update(error=None, left=left)
        if new:
            print(f"  Spieldauern (IGDB): {new} Spiele ergänzt" + (f", {left} folgen" if left else ""))
    except Exception as e:
        hltb_state["error"] = err_text(e)
        print("  HowLongToBeat:", hltb_state["error"])
    finally:
        hltb_state["running"] = False
        cloud_state["pending"] = True


def steamx_worker(cfg, games):
    if steamx_state["running"]:
        return
    steamx_state.update(running=True)
    try:
        sid = resolve_steam_id(cfg["steam_api_key"].strip(), cfg["steam_id"])
        c = steamx.update(STEAMX_PATH, cfg["steam_api_key"].strip(), sid, games, status=steamx_state)
        steamx_state["error"] = None
        if notify_prefs(cfg)["cats"].get("sales", True):
            sent = set(kv_get("notified_sales", []))
            names = {w["appid"]: (c.get("prices", {}).get(w["appid"]) or {}) for w in c.get("wishlist", [])}
            fresh = [a for a in c.get("new_sales", []) if a not in sent]
            for a in fresh[:3]:
                p = names.get(a, {})
                alert("sales", "Wunschliste im Angebot", f"{p.get('name') or 'Ein Spiel'}: -{p.get('discount')} % für {p.get('fmt') or '?'}",
                             f"https://store.steampowered.com/app/{a}/")
            if fresh:
                kv_set("notified_sales", list(sent | set(fresh))[-500:])
    except Exception as e:
        steamx_state["error"] = err_text(e)
        print("  Steam-Extras:", steamx_state["error"])
    finally:
        steamx_state["running"] = False
        cloud_state["pending"] = True


scores_state = {"running": False, "error": None}


def scores_worker(games):
    """Steam-Rezensionen und Metascore für alle Spiele mit Steam-ID (kein API-Key nötig)."""
    if scores_state["running"]:
        return
    scores_state["running"] = True
    try:
        played = sorted([g for g in games if g.get("steam_appid")], key=lambda g: -(g.get("minutes") or 0))
        steamx.update_scores(SCORES_PATH, [g["steam_appid"] for g in played], status=scores_state)
        scores_state["error"] = None
        print(f"  Steam-Wertungen: {len(read_json(SCORES_PATH, {}).get('scores', {}))} Spiele")
    except Exception as e:
        print("  Steam-Wertungen fehlgeschlagen:", err_text(e))
        scores_state["error"] = err_text(e)
    finally:
        scores_state["running"] = False
        cloud_state["pending"] = True


extras_state = {"running": False, "error": None}


def extras_worker(cfg):
    """Trophäen (bei jedem Abgleich) und Gratis-Spiele (alle 6 Std)."""
    if extras_state["running"]:
        return
    extras_state["running"] = True
    try:
        x = read_json(EXTRAS_PATH, {})
        if cfg["psn_npsso"].strip():
            try:
                x["trophies"] = extras.psn_trophies(psn_access_token(cfg["psn_npsso"]))
                x["trophies_at"] = now_iso()
            except Exception as e:
                extras_state["error"] = "Trophäen: " + err_text(e)
        if time.time() - (parse_iso(x.get("free_at")) or 0) > 3 * 3600 or any("source" not in f for f in x.get("free") or []):
            fg = extras.free_games()
            if fg:
                x["free"] = fg
                x["free_at"] = now_iso()
        write_json(EXTRAS_PATH, x)
        cloud_state["pending"] = True
    finally:
        extras_state["running"] = False


def first_tracking_day():
    con = history_con()
    row = con.execute("SELECT MIN(day) FROM snapshots").fetchone()
    con.close()
    return row[0] if row else None


def finalize_archives(games):
    """Abgelaufene Monate/Jahre im Wrapped-Archiv mit den gemessenen Minuten bis zum Ende ergänzen und einfrieren.
    (Das Archiv speichert die Oberfläche; war sie zum Monats-/Jahresende nicht offen, fehlen sonst die letzten Tage.)"""
    arch = kv_get("wrapped", {}) or {}
    today = dt.date.today()
    keymap = {g["id"]: (norm_title(g["title"]), g["device"]) for g in games}
    con = history_con()
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
    changed = False
    # Zeiträume ohne Eintrag (App in dem Monat/Jahr nie geöffnet) aus den Tagesständen nachtragen
    if days:
        first = dt.date.fromisoformat(days[0])
        y, m = first.year, first.month
        while (y, m) < (today.year, today.month):
            for k in (f"{y}-{m:02d}",) + ((str(y),) if m == 12 and y < today.year else ()):
                if k not in arch:
                    arch[k] = {"saved": f"{k}-01T00:00:00" if len(k) == 7 else f"{k}-01-01T00:00:00", "estimated": False, "games": {}, "auto": True}
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    for k, a in arch.items():
        if a.get("final") or not a.get("saved"):
            continue
        if len(k) == 4:
            end = dt.date(int(k) + 1, 1, 1)
        else:
            y, m = int(k[:4]), int(k[5:7])
            end = dt.date(y + (m == 12), m % 12 + 1, 1)
        if today < end:
            continue
        start = dt.date(int(k[:4]), 1, 1) if len(k) == 4 else dt.date(int(k[:4]), int(k[5:7]), 1)
        pre = [d for d in days if d < start.isoformat()]
        b0 = [d for d in days if d < end.isoformat()]
        if pre and b0 and not a.get("estimated"):
            # Zeitraum komplett aufgezeichnet: exakt aus den Tagesständen (Ende Vortag bis Ende letzter Tag)
            q = "SELECT id, minutes FROM snapshots WHERE day = ?"
            x0, x1 = dict(con.execute(q, (pre[-1],)).fetchall()), dict(con.execute(q, (b0[-1],)).fetchall())
            exact = {}
            for gid, m1 in x1.items():
                delta = m1 - x0.get(gid, 0 if gid not in x0 else m1)
                if delta > 0 and gid in keymap:
                    key, dev = keymap[gid]
                    e = exact.setdefault(key, [0, {}])
                    e[0] += delta
                    e[1][dev] = e[1].get(dev, 0) + delta
            for key, e in (a.get("games") or {}).items():  # Korrekturen/Konsolenzeiten aus dem Archiv nicht verlieren
                if key not in exact:
                    exact[key] = e
            a["games"] = exact
            a["final"] = True
            changed = True
            continue
        if a.get("auto"):
            continue  # nicht komplett aufgezeichnet: lieber kein Wrapped als ein falsches
        saved_day = a["saved"][:10]
        a0 = [d for d in days if d <= saved_day]
        if a0 and b0 and b0[-1] > a0[-1]:
            q = "SELECT id, minutes FROM snapshots WHERE day = ?"
            x0, x1 = dict(con.execute(q, (a0[-1],)).fetchall()), dict(con.execute(q, (b0[-1],)).fetchall())
            for gid, m1 in x1.items():
                delta = m1 - x0.get(gid, m1)
                if delta > 0 and gid in keymap:
                    key, dev = keymap[gid]
                    e = a.setdefault("games", {}).setdefault(key, [0, {}])
                    e[0] += delta
                    e[1][dev] = e[1].get(dev, 0) + delta
        a["final"] = True
        changed = True
    con.close()
    for k in [k for k, a in arch.items() if a.get("auto") and not a.get("final")]:
        del arch[k]
    for k in [k for k, a in arch.items() if a.get("final") and not a.get("games")]:
        del arch[k]  # nichts gespielt: kein leeres Wrapped anbieten
        changed = True
    months = sorted(k for k in arch if len(k) == 7)
    for k in months[:-6]:  # wie in der App: die letzten 6 Monate behalten, Jahre immer
        del arch[k]
        changed = True
    if changed:
        kv_set("wrapped", arch)
    return changed


media_state = {"running": False, "error": None, "cover_error": None}


def media_worker(cfg, games):
    """Bibliothekswert (IsThereAnyDeal) und fehlende Cover (SteamGridDB)."""
    if media_state["running"]:
        return
    media_state.update(running=True, phase=None, done=0, total=0)
    try:
        itad, sg, rawg = (cfg.get("itad_key") or "").strip(), (cfg.get("sgdb_key") or "").strip(), (cfg.get("rawg_key") or "").strip()
        if rawg:
            titles = {}
            for g in sorted(games, key=lambda g: (g.get("device") != "PC", not g.get("steam_appid"))):
                if g.get("hidden"):
                    continue
                k = norm_title(g["title"])
                if k and k not in titles:
                    titles[k] = g["title"]
            try:
                n = media.update_metacritic(MEDIA_PATH, rawg, titles, status=media_state)
                media_state["mc_error"] = None
                if n:
                    print(f"  Metacritic (RAWG): {n} Wertungen gefunden")
            except Exception as e:
                media_state["mc_error"] = err_text(e)
                print("  RAWG:", media_state["mc_error"])
        if itad:
            wanted = {}
            for g in sorted(games, key=lambda g: g.get("device") != "PC"):
                if g.get("family"):
                    continue
                # Konsolenspiele: über den Titel (PC-Preis als Schätzung für den Gesamtwert)
                k = norm_title(g["title"])
                if k and (k not in wanted or (g.get("steam_appid") and not wanted[k][1])):
                    wanted[k] = (g["title"], g.get("steam_appid"))
            try:
                media.MODE["steam_only"] = bool(cfg.get("prices_steam_only", True))
                n = media.update_prices(MEDIA_PATH, itad, wanted, status=media_state)
                media_state["error"] = None
                if n:
                    print(f"  Bibliothekswert: {n} Preise aktualisiert")
                sx = read_json(STEAMX_PATH, {})
                wl_ids = [str(w.get("appid")) for w in sx.get("wishlist") or []]
                if wl_ids:
                    deals = media.update_wishlist(MEDIA_PATH, itad, wl_ids, status=media_state)
                    if notify_prefs(cfg)["cats"].get("lows", True):
                        notify_lows(deals, sx)
            except Exception as e:
                media_state["error"] = err_text(e)
                print("  IsThereAnyDeal:", media_state["error"])
        if sg:
            h = hltb.load(APP_DB)
            groups = {}
            for g in games:
                k = norm_title(g["title"])
                if not k:
                    continue
                e = groups.setdefault(k, [g["title"], False])
                if g.get("device") == "PC" and e[0] != g["title"] and not g.get("steam_appid"):
                    e[0] = g["title"]
                if any(not str(c).startswith("/api/img") for c in g.get("covers") or []) or (h.get(k) or {}).get("image"):
                    e[1] = True
            wanted = {k: t for k, (t, ok) in groups.items() if not ok}
            try:
                n = media.update_covers(MEDIA_PATH, sg, wanted, status=media_state)
                media_state["cover_error"] = None
                if n:
                    print(f"  SteamGridDB: {n} Cover gefunden")
            except Exception as e:
                media_state["cover_error"] = err_text(e)
                print("  SteamGridDB:", media_state["cover_error"])
    finally:
        media_state["running"] = False
        cloud_state["pending"] = True


partner_state = {"last": 0, "error": None, "at": None}


def partner_summary(cfg, stable=False):
    """Was „Wir zwei“ mit dem Partner teilt: pro Spiel Titel, Spielzeiten (gesamt/Jahr/Monat), Besitz, Cover, Koop.
    Wird mit dem gemeinsamen Partner-Code verschlüsselt hochgeladen."""
    games = load_cache().get("games", [])
    pm_y, _ = period_minutes(games, "year")
    pm_m, _ = period_minutes(games, "month")
    xc = media.summary(MEDIA_PATH).get("xcovers") or {}
    meta = load_meta()
    groups = {}
    for g in sorted(games, key=lambda g: g.get("device") != "PC"):
        k = norm_title(g["title"])
        if not k or (meta.get(k) or {}).get("hide"):
            continue
        e = groups.setdefault(k, {"t": g["title"], "m": 0, "y": 0, "mo": 0, "own": False, "c": None, "mp": False, "d": set()})
        e["m"] += int(g.get("minutes") or 0)
        e["y"] += int((pm_y or {}).get(g["id"], 0))
        e["mo"] += int((pm_m or {}).get(g["id"], 0))
        e["d"].add(g.get("device") or "PC")
        if not g.get("family"):
            e["own"] = True
        if not e["c"]:
            e["c"] = next((c for c in g.get("covers") or [] if not str(c).startswith("/api/img")), None)
        if any(re.search(r"multi|co-?op|koop", str(f), re.I) for f in g.get("features") or []):
            e["mp"] = True
    rows = []
    for k, e in sorted(groups.items(), key=lambda kv: -kv[1]["m"])[:2500]:
        rows.append([k, e["t"], e["m"], e["y"], e["mo"], int(e["own"]), e["c"] or xc.get(k), int(e["mp"]), sorted(e["d"])])
    out = {"v": 1, "name": (cfg.get("user_name") or "").strip() or "Partner", "year_n": dt.date.today().year,
           "total": sum(r[2] for r in rows), "year": sum(r[3] for r in rows), "month": sum(r[4] for r in rows), "games": rows}
    if not stable:
        out["at"] = now_iso()
    return out


def fetch_partner(cfg):
    """Holt die Daten des Partners (verschlüsselt in dessen GitHub-Pages-Repository)."""
    url, code = (cfg.get("partner_url") or "").strip(), (cfg.get("partner_code") or "").strip()
    if not url or not code or time.time() - partner_state["last"] < 1800:
        return
    partner_state["last"] = time.time()
    try:
        if not url.startswith("https://"):
            url = "https://" + url
        req = urllib.request.Request(url.rstrip("/") + "/partner.enc?t=" + str(int(time.time())), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            blob = r.read()
        data = cloud.decrypt(blob, code)
        if data != kv_get("partner", None):
            kv_set("partner", data)
            cloud_state["pending"] = True
        partner_state.update(error=None, at=data.get("at"))
    except urllib.error.HTTPError as e:
        partner_state["error"] = "Beim Partner liegt noch nichts (sein PC muss einmal hochgeladen haben)." if e.code == 404 else err_text(e)
    except ValueError:
        partner_state["error"] = "Partner-Code passt nicht – bei beiden muss derselbe Code eingetragen sein."
    except Exception as e:
        partner_state["error"] = err_text(e)


SHARE_KEYS = ("igdb_client_id", "igdb_client_secret", "itad_key", "sgdb_key", "rawg_key")


def error_report(cfg):
    """Fehlerbericht ohne Keys und ohne Spieldaten – zum Weiterschicken."""
    lines = [f"Gaming Recap {VERSION} (Release {updater.local_release(BASE, VERSION)})", f"Erstellt: {now_iso()}",
             f"System: {sys.platform}, Python {sys.version.split()[0]}", f"Datenordner: {'Programmordner' if DATA_DIR.startswith(BASE) else 'AppData'}", ""]
    pub = public_config(cfg)
    lines.append("Einstellungen (ohne Geheimnisse):")
    for k in sorted(pub):
        if k in ("steam_id", "gh_repo", "playnite_export", "push_topic", "partner_url", "user_name"):
            pub[k] = "(gesetzt)" if pub[k] else "(leer)"
        lines.append(f"  {k}: {pub[k]}")
    lines += ["", "Systemcheck:"] + [f"  [{h['level']}] {h['k']}: {h['msg']}" for h in health(cfg)]
    lines += ["", f"Update: {json.dumps({k: v for k, v in update_state.items()}, default=str)}",
              f"Cloud: {json.dumps({k: cloud_state.get(k) for k in ('last_push', 'error', 'app_hash', 'pub_release')}, default=str)}"]
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            log = f.readlines()[-300:]
    except OSError:
        log = ["(kein Protokoll)"]
    lines += ["", "Protokoll (letzte 300 Zeilen):"] + [re.sub(r"(key|token|npsso|secret|pass)=[^&\s]+", r"\1=***", l.rstrip()) for l in log]
    return "\n".join(lines)


update_state = {"checking": False, "last_check": 0, "latest": None, "changed": 0, "error": None, "deferred": False}


def update_source(cfg):
    """Woher Updates kommen: Einstellung oder quelle.json im Programmordner (liegt dem Installationspaket bei)."""
    if (cfg.get("update_url") or "").strip():
        return cfg["update_url"].strip()
    q = read_json(os.path.join(BASE, "quelle.json"), {})
    return (q.get("update_url") or "").strip()


def update_worker(cfg, force=False, apply_now=None):
    """Einmal am Tag nach Updates schauen und (falls eingeschaltet) installieren."""
    url = update_source(cfg)
    if not url or cfg.get("publish_updates") or update_state["checking"]:
        return
    if not force and time.time() - update_state["last_check"] < 20 * 3600 and not update_state.get("deferred"):
        return
    if update_state.get("deferred") and not force and time.time() - update_state["last_check"] < 3600:
        return
    update_state.update(checking=True, error=None, last_check=time.time())
    try:
        man, changed = updater.check(url, BASE, VERSION)
        blocked = read_json(os.path.join(DATA_DIR, "update_gesperrt.json"), {}).get("release")
        update_state.update(latest=man.get("release"), latest_version=man.get("version"), changed=len(changed))
        if not changed or man.get("release") == blocked:
            update_state["deferred"] = False
            return
        do = apply_now if apply_now is not None else cfg.get("auto_update", True)
        if not do:
            return
        if TRACKER.current and not force:  # nicht mitten im Spiel neu starten
            update_state["deferred"] = True
            return
        updater.apply(url, BASE, DATA_DIR, man, changed)
        print(f"  Update: {len(changed)} Dateien auf Release {man['release']} – starte neu")
        update_state.update(deferred=False, applied=man["release"])
        restart_server(guard=True)
    except Exception as e:
        update_state["error"] = err_text(e)
        print("  Update:", update_state["error"])
    finally:
        update_state["checking"] = False


def notify_lows(deals, sx):
    """Push, wenn ein Wunschlisten-Spiel auf dem Tiefstpreis aller Zeiten ist (pro Preis nur einmal)."""
    seen = kv_get("atl_seen", {}) or {}
    names = {str(a): (p or {}).get("name") for a, p in (sx.get("prices") or {}).items()}
    hits = []
    for a, d in deals.items():
        c, low = d.get("c"), d.get("l")
        if c is None or low is None or not d.get("cut") or c > low:
            continue
        if seen.get(a) == c:
            continue
        seen[a] = c
        hits.append((a, d))
    for a, d in hits[:3]:
        alert("lows", "🔥 Tiefstpreis auf deiner Wunschliste",
                     f"{names.get(a) or 'Ein Spiel'}: " + f"{d['c'] / 100:.2f} € ".replace(".", ",")
                     + f"bei {d.get('shop') or 'Steam'} (−{d.get('cut')} %) – so günstig war es noch nie.",
                     f"https://store.steampowered.com/app/{a}/")
    if hits:
        kv_set("atl_seen", dict(list(seen.items())[-500:]))


def health(cfg):
    """Systemcheck: was läuft, was nicht – mit Hinweis, wie man es behebt."""
    out = []
    add = lambda k, ok, msg, fix=None, level=None: out.append({"k": k, "ok": ok, "msg": msg, "fix": fix, "level": level or ("ok" if ok else "error")})
    cache = load_cache()
    src = cache.get("sources", {})
    ls = parse_iso(cache.get("last_sync"))
    hours = max(1, float(cfg.get("sync_hours") or 1))
    if not ls:
        add("Abgleich", False, "Noch kein Abgleich gelaufen.", "Gaming Recap neu starten.")
    elif time.time() - ls > (hours + 1.5) * 3600 and not state["syncing"]:
        add("Abgleich", False, f"Letzter Abgleich vor {int((time.time() - ls) / 3600)} Std.", "Gaming Recap neu starten.")
    else:
        add("Abgleich", True, f"alle {hours:g} Std, zuletzt {time.strftime('%H:%M', time.localtime(ls))}")
    for name, fix in (("Steam", "Steam-Key und Profil in den Einstellungen prüfen; Spieldetails in Steam auf „öffentlich“."),
                      ("PlayStation", "Neuen NPSSO-Token holen (Einstellungen › PlayStation) – er läuft etwa alle 2 Monate ab."),
                      ("Xbox", "In Playnite die Xbox-Bibliothek verbinden."),
                      ("Playnite", "Playnite starten; Erweiterung in den Einstellungen neu installieren.")):
        v = src.get(name)
        if v:
            add(name, v.get("ok"), v.get("info") or (f"{v.get('count')} Spiele" if v.get("ok") and v.get("count") is not None else v.get("error") or "ok"),
                None if v.get("ok") else fix)
    try:
        age = time.time() - os.path.getmtime(cfg["playnite_export"])
        if age > 3 * 86400:
            add("Playnite-Export", False, f"seit {int(age / 86400)} Tagen nicht aktualisiert.",
                "Playnite öffnen (am besten mit Windows starten lassen), damit Epic, Ubisoft, EA, GOG und Xbox aktuell bleiben.")
    except OSError:
        pass
    if cfg.get("hltb"):
        if hltb_state.get("error"):
            add("Spieldauern (IGDB)", False, hltb_state["error"], "IGDB-Zugang (Twitch Client-ID/Secret) prüfen.")
        elif hltb_state.get("missing"):
            add("Spieldauern (IGDB)", False, "kein Zugang eingetragen", "IGDB Client-ID und Secret eintragen.", "warn")
    for nm, st in (("Steam-Extras", steamx_state), ("Steam-Wertungen", scores_state), ("Trophäen & Gratis-Spiele", extras_state)):
        if st.get("error"):
            add(nm, False, st["error"], "Meist vorübergehend – wird beim nächsten Abgleich erneut versucht.", "warn")
    if cloud_ready(cfg):
        lp = parse_iso(cloud_state.get("last_push"))
        if cloud_state.get("error"):
            add("Handy-Cloud", False, cloud_state["error"], "GitHub-Token und Repository prüfen (Token abgelaufen?).")
        elif lp and time.time() - lp > 3 * 3600:
            add("Handy-Cloud", False, f"seit {int((time.time() - lp) / 3600)} Std nichts hochgeladen.", "Gaming Recap neu starten.", "warn")
        else:
            add("Handy-Cloud", True, "läuft" + (f", zuletzt {time.strftime('%H:%M', time.localtime(lp))}" if lp else ""))
    if cfg.get("tracker") and os.name == "nt":
        if time.time() - (TRACKER.last_ok or 0) > 180:
            add("Session-Tracker", False, "reagiert nicht.", "Gaming Recap neu starten.")
        else:
            add("Session-Tracker", True, "aktiv" + (f" – spielt gerade {TRACKER.current[0]['title']}" if TRACKER.current else ""))
    if media_state.get("error"):
        add("Bibliothekswert (IsThereAnyDeal)", False, media_state["error"], "API-Key in den Einstellungen › Extras prüfen.")
    elif not (cfg.get("itad_key") or "").strip():
        add("Bibliothekswert (IsThereAnyDeal)", False, "kein Key eingetragen – Tiefstpreise fehlen", "Einstellungen › Extras › IsThereAnyDeal.", "info")
    if media_state.get("mc_error"):
        add("Metacritic (RAWG)", False, media_state["mc_error"], "RAWG-Key in den Einstellungen › Preise & Cover prüfen.")
    elif not (cfg.get("rawg_key") or "").strip():
        add("Metacritic (RAWG)", False, "kein Key eingetragen – Wertungen kommen aus IGDB/Playnite", "Einstellungen › Preise & Cover › RAWG.", "info")
    if media_state.get("cover_error"):
        add("Cover (SteamGridDB)", False, media_state["cover_error"], "API-Key in den Einstellungen › Extras prüfen.")
    elif not (cfg.get("sgdb_key") or "").strip():
        add("Cover (SteamGridDB)", False, "kein Key eingetragen – einige Cover fehlen", "Einstellungen › Extras › SteamGridDB.", "info")
    if cfg.get("partner_url") and partner_state.get("error"):
        add("Wir zwei", False, partner_state["error"], "Partner-Adresse und Partner-Code in den Einstellungen › Wir zwei prüfen.", "warn")
    af = autostart_file()
    if af and not os.path.exists(af):
        add("Autostart", False, "Gaming Recap startet nicht mit Windows.", "Einstellungen › Extras › „Mit Windows starten“ anhaken.", "warn")
    if not (cfg.get("push_topic") or "").strip():
        add("Push aufs Handy", False, "nicht eingerichtet", "Einstellungen › Push aufs Handy.", "info")
    return out


def check_health(cfg):
    """Neue Probleme einmal melden (Windows + Handy)."""
    errs = {f"{h['k']}: {h['msg']}" for h in health(cfg) if h["level"] == "error"}
    known = set(kv_get("health_notified", []) or [])
    new = errs - known
    if new:
        alert("health", "Gaming Recap: Problem", "; ".join(sorted(new))[:220])
    if errs != known:
        kv_set("health_notified", sorted(errs))


def offline_watchdog(cfg, force=False):
    """Totmannschalter: Der PC plant bei ntfy eine Nachricht in 48 Std und erneuert sie alle 6 Std.
    Läuft der PC nicht mehr, wird sie nicht mehr abgesagt – und kommt dann von ntfy, ganz ohne PC."""
    topic = (cfg.get("push_topic") or "").strip()
    prefs = notify_prefs(cfg)
    on = bool(topic) and prefs.get("phone", True) and (prefs.get("cats") or {}).get("offline", True)
    last = kv_get("offline_sid", None)
    if on and last and time.time() - last[1] < 6 * 3600 and not force:
        return
    if last:
        notify.cancel(last[0], last[2] if len(last) > 2 else topic)
        kv_set("offline_sid", None)
    if not on:
        return
    when = dt.datetime.now() + dt.timedelta(hours=48)
    q = prefs.get("quiet")
    if q and in_quiet(prefs, when):  # nicht mitten in der Nacht
        when = when.replace(hour=q[1], minute=5, second=0)
        if when < dt.datetime.now() + dt.timedelta(hours=40):
            when += dt.timedelta(days=1)
        when = min(when, dt.datetime.now() + dt.timedelta(hours=71))
    sid = f"gr-offline-{int(time.time())}"
    ok = notify.push("⚠️ Dein PC meldet sich nicht", "Gaming Recap hat seit 2 Tagen nichts hochgeladen – Sessions werden gerade nicht erfasst. "
                     "Läuft der PC? Falls ja: in der App unter Einstellungen › System neu starten.",
                     cloud_state.get("url"), topic=topic, delay=int(when.timestamp()), sid=sid)
    if ok:
        kv_set("offline_sid", [sid, time.time(), topic])


def check_expiry(cfg):
    """Warnt rechtzeitig, bevor GitHub-Token oder PlayStation-Code ablaufen (einmal je Stufe)."""
    warned = kv_get("expiry_warned", {}) or {}
    now = time.time()
    msgs = []
    if cloud_ready(cfg) and now - (cloud_state.get("token_exp_checked") or 0) > 86400:
        try:
            exp = cloud.GitHub(cfg["gh_repo"], cfg["gh_token"]).token_expiry()
            cloud_state["token_exp"] = exp
        except Exception:
            exp = cloud_state.get("token_exp")
        cloud_state["token_exp_checked"] = now
    exp = cloud_state.get("token_exp")
    if exp:
        try:
            t = dt.datetime.strptime(exp.replace(" UTC", "").strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
            days = (t - now) / 86400
            for stage in (14, 3):
                key = f"gh-{exp}-{stage}"
                if days <= stage and key not in warned:
                    warned[key] = now
                    msgs.append(("🔑 GitHub-Token läuft bald ab",
                                 f"Am {time.strftime('%d.%m.%Y', time.localtime(t))} läuft dein GitHub-Token ab – danach bekommt das Handy keine neuen Daten. "
                                 "Auf github.com › Settings › Developer settings › Personal access tokens neu erzeugen und am PC in den Einstellungen eintragen."))
                    break
        except ValueError:
            pass
    ps = cfg.get("psn_set_at")
    if (cfg.get("psn_npsso") or "").strip() and ps:
        age = (now - ps) / 86400
        key = f"psn-{int(ps)}"
        if age >= 50 and key not in warned:
            warned[key] = now
            msgs.append(("🎮 PlayStation-Zugang läuft bald ab",
                         "Der PlayStation-Code (NPSSO) hält etwa 2 Monate und ist bald fällig. Hol dir einen neuen: playstation.com anmelden, "
                         "dann ca.account.sony.com/api/v1/ssocookie öffnen und den Wert am PC unter Einstellungen › PlayStation eintragen."))
    for t, b in msgs:
        alert("health", t, b)
    if msgs:
        kv_set("expiry_warned", dict(list(warned.items())[-50:]))


def check_notifications(cfg):
    """Pausen-Erinnerung, Gratis-Spiele, Releases, Wochen- und Monats-Recap."""
    prefs = notify_prefs(cfg)
    cats = prefs.get("cats") or {}
    flush_push_queue()
    ph = float(prefs.get("pause_h") or 0)
    if ph > 0 and cats.get("pause", True):
        for p in TRACKER.current:
            tag = f"{p['key']}:{p['since']}"
            if time.time() - p["since"] >= ph * 3600 and tag not in notified["pause"]:
                notified["pause"].add(tag)
                alert("pause", "Zeit für eine Pause?", f"Du spielst {p['title']} seit {ph:g} Stunden am Stück.")
    x = read_json(EXTRAS_PATH, {})
    if cats.get("free", True) and x.get("free"):
        seen = set(kv_get("free_seen", []) or [])
        new = [f for f in x["free"] if f["kind"] == "now" and f["key"] not in seen]
        if new:
            owned = {norm_title(g["title"]) for g in load_cache().get("games", [])}
            kv_set("free_seen", sorted(seen | {f["key"] for f in new})[-200:])
            fresh = [f for f in new if f["key"] not in owned]
            if fresh:
                alert("free", "Gratis bei Epic", ", ".join(f["title"] for f in fresh[:3]) + " – jetzt kostenlos holen.", fresh[0].get("url"))
    if cats.get("release", True):
        sx = read_json(STEAMX_PATH, {})
        today = dt.date.today()
        seen = set(kv_get("release_seen", []) or [])
        for w in sx.get("wishlist") or []:
            p = (sx.get("prices") or {}).get(str(w.get("appid"))) or (sx.get("prices") or {}).get(w.get("appid")) or {}
            rel = p.get("release")
            if rel and dt.date.fromtimestamp(rel) == today and str(w.get("appid")) not in seen:
                seen.add(str(w.get("appid")))
                kv_set("release_seen", sorted(seen)[-300:])
                alert("release", "Heute erschienen", f"{p.get('name') or 'Ein Spiel von deiner Wunschliste'} ist ab heute erhältlich"
                             + (f" – {p.get('fmt')}" if p.get("fmt") else "") + ".",
                             f"https://store.steampowered.com/app/{w.get('appid')}")
    if cats.get("yearly", True):
        now = dt.datetime.now()
        base = cloud_state.get("url") or f"http://localhost:{PORT}/"
        if now.month == 12 and now.day <= 7 and now.hour >= 10 and kv_get("season_sent") != f"{now.year}-dec":
            kv_set("season_sent", f"{now.year}-dec")
            pm, _ = period_minutes(load_cache().get("games", []), "year")
            h = sum((pm or {}).values()) / 60
            alert("yearly", f"🎬 Dein Gaming-Jahr {now.year} ist da",
                  (f"Bisher {h:,.0f} Stunden. ".replace(",", ".") if h else "") + "Dein Jahres-Wrapped ist fertig – tippe für deine Story.",
                  f"{base}#/wrapped/year")
        if now.month == 1 and now.day <= 7 and now.hour >= 10 and kv_get("season_sent") != f"{now.year - 1}-final":
            kv_set("season_sent", f"{now.year - 1}-final")
            alert("yearly", f"🏁 {now.year - 1} ist abgeschlossen", f"Dein komplettes Gaming-Jahr {now.year - 1} steht fest. Tippe für das finale Wrapped.",
                  f"{base}#/wrapped/arch:{now.year - 1}")
    if cats.get("weekly", True) or cats.get("monthly", True):
        now = dt.datetime.now()
        week = now.strftime("%G-%V")
        if cats.get("weekly", True) and now.weekday() == 0 and now.hour >= 9 and kv_get("weekly_sent") != week:
            kv_set("weekly_sent", week)
            ss = tracker.load_sessions(APP_DB, int(time.time()) - 7 * 86400)
            if ss:
                mins = sum(s[2] for s in ss) / 60
                per = {}
                for k, _, sec in ss:
                    per[k] = per.get(k, 0) + sec
                top = max(per, key=per.get)
                names = {norm_title(g["title"]): g["title"] for g in load_cache().get("games", [])}
                alert("weekly", "Dein Wochen-Recap", f"{mins / 60:.1f} Std am PC gezockt, meistens {names.get(top, top)}. "
                             f"{len(ss)} Sessions an {len({time.strftime('%j', time.localtime(s[1])) for s in ss})} Tagen.")
        today = dt.date.today()
        prev = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        mkey = prev.strftime("%Y-%m")
        if cats.get("monthly", True) and today.day <= 3 and now.hour >= 9 and kv_get("monthly_sent") != mkey:
            kv_set("monthly_sent", mkey)
            names = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                     "Oktober", "November", "Dezember"]
            arch = (kv_get("wrapped", {}) or {}).get(mkey) or {}
            games = arch.get("games") or {}
            mins = sum(x[0] for x in games.values()) if games else month_minutes(prev, today.replace(day=1))
            top = max(games.items(), key=lambda kv: kv[1][0]) if games else None
            tnames = {norm_title(g["title"]): g["title"] for g in load_cache().get("games", [])}
            text = f"{mins / 60:.0f} Stunden gezockt" if mins else "Dein Monat in Zahlen"
            if top:
                text += f", am meisten {tnames.get(top[0], top[0])} ({top[1][0] / 60:.0f} Std)"
            if games:
                text += f" in {len(games)} Spielen"
            text += ". Tippe für dein Monats-Wrapped."
            base = cloud_state.get("url") or f"http://localhost:{PORT}/"
            alert("monthly", f"🎬 Dein {names[prev.month - 1]}-Wrapped ist da", text, f"{base}#/wrapped/arch:{mkey}")


def month_minutes(start, end):
    """Gemessene Spielzeit zwischen zwei Tagen aus dem Verlauf (0, wenn nicht genug Daten)."""
    con = history_con()
    try:
        days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
        a = [d for d in days if d < start.isoformat()]   # Stand am Ende des Vortags
        b = [d for d in days if d < end.isoformat()]
        if not a or not b or a[-1] == b[-1]:
            return 0
        q = "SELECT id, minutes FROM snapshots WHERE day = ?"
        x, y = dict(con.execute(q, (a[-1],)).fetchall()), dict(con.execute(q, (b[-1],)).fetchall())
        return sum(max(0, m - x.get(i, m)) for i, m in y.items())
    finally:
        con.close()


def sync_async():
    threading.Thread(target=run_sync, daemon=True).start()


# ================================================================ Cloud

def app_files():
    files = {".nojekyll": b""}
    for name in ("index.html", "sw.js", "manifest.webmanifest", "icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        p = os.path.join(WEB_DIR, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                files[name] = f.read()
    h = hashlib.sha256(b"".join(files[k] for k in sorted(files))).hexdigest()[:12]
    if "sw.js" in files:
        files["sw.js"] = files["sw.js"].replace(b"__APPHASH__", h.encode())
    return files, h


def clean_tiers(games):
    """Tier-Liste erst ab 5 Stunden: Einordnungen von Spielen darunter entfernen."""
    if not games:
        return 0
    tot = {}
    for g in games:
        k = norm_title(g["title"])
        tot[k] = tot.get(k, 0) + int(g.get("minutes") or 0)
    con = app_con()
    keys = [k for (k,) in con.execute("SELECT key FROM meta WHERE tier IS NOT NULL") if tot.get(k, 0) < 300]
    with con:
        con.executemany("UPDATE meta SET tier = NULL, updated = ? WHERE key = ?", [(now_iso(), k) for k in keys])
    con.close()
    return len(keys)


def build_snapshot(cfg, gh):
    cache = load_cache()
    try:
        clean_tiers(cache.get("games", []))
    except Exception as e:
        print("  Tier-Liste:", e)
    games = []
    for g in cache.get("games", []):
        g = {k: v for k, v in g.items() if k not in ("install_dir",)}
        g["covers"] = [c for c in g.get("covers", []) if not c.startswith("/api/img")]
        g["banners"] = [c for c in g.get("banners", []) if not c.startswith("/api/img")]
        games.append(g)
    periods = {}
    for p in ("year", "month", "week"):
        pm, info = period_minutes(cache.get("games", []), p)
        periods[p] = {"pm": pm, "info": info}
    snap = {"v": 3, "last_sync": cache.get("last_sync"), "sources": cache.get("sources", {}), "games": games,
            "periods": periods, "gh": {"repo": gh.repo, "token": cfg["gh_token"].strip(), "branch": gh.branch}}
    snap.update(shared_data(cfg, cache.get("games", [])))
    return snap


def shared_data(cfg, games):
    """Daten, die PC-Ansicht und Handy-App gleichermaßen bekommen."""
    return {
        "meta": load_meta(), "hltb": hltb.load(APP_DB),
        "sessions": tracker.load_sessions(APP_DB, int(time.time()) - 800 * 86400),
        "csessions": load_console_sessions(int(time.time()) - 800 * 86400),
        "status_log": load_status_log(), "goals": kv_get("goals", {}), "goals_updated": kv_updated("goals"),
        "year_overrides": kv_get("year_overrides", {}), "wrapped_archive": kv_get("wrapped", {}), "notpc": kv_get("notpc", {}),
        "tracking_since": first_tracking_day(), "health": health(cfg),
        "kv_updated": {k: kv_updated(k) for k in ("goals", "year_overrides", "wrapped", "notpc", "snotes", "covers", "paid", "notify", "critic")},
        "critic": kv_get("critic", {}),
        "notify": notify_prefs(cfg), "push_ready": bool((cfg.get("push_topic") or "").strip()),
        "covers": kv_get("covers", {}), "paid": kv_get("paid", {}),
        "partner": kv_get("partner", None) if (cfg.get("partner_url") and cfg.get("partner_code")) else None,
        "me_name": (cfg.get("user_name") or "").strip(),
        "trophies": read_json(EXTRAS_PATH, {}).get("trophies") or [],
        "free_games": read_json(EXTRAS_PATH, {}).get("free") or [],
        "snotes": kv_get("snotes", {}),
        **media.summary(MEDIA_PATH),
        "steam": steamx.summary(dict(read_json(STEAMX_PATH, {}), scores=read_json(SCORES_PATH, {}).get("scores", {})), games),
        "prefs": {"watts": cfg.get("watts") or 350, "kwh_price": cfg.get("kwh_price") or 0.35,
                  "backlog_stores": cfg.get("backlog_stores") or DEFAULT_CONFIG["backlog_stores"]},
    }


def cloud_ready(cfg):
    return cfg.get("cloud_enabled") and all(str(cfg.get(k, "")).strip() for k in ("gh_repo", "gh_token", "cloud_pass"))


# ---------------------------------------------------------------- Backup (verschlüsselt im GitHub-Repo)

BACKUP_TABLES = ("meta", "status_log", "kv", "sessions", "console_sessions")


def build_backup():
    con = console_con()
    tracker.init_db(con)
    out = {"v": 1, "at": now_iso(), "tables": {}}
    for t in BACKUP_TABLES:
        cur = con.execute(f"SELECT * FROM {t}")
        cols = [d[0] for d in cur.description]
        out["tables"][t] = {"cols": cols, "rows": cur.fetchall()}
    con.close()
    # Verlauf: nur die Tage, die für Jahres-/Monats-Auswertungen gebraucht werden
    hc = history_con()
    days = [r[0] for r in hc.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
    keep = set(days[:1] + days[-1:])
    for d in days:
        month_start = d[:8] + "01"
        before = [x for x in days if x < month_start]   # Stand am Ende des Vormonats (Basis für Monats-/Jahreswerte)
        if before:
            keep.add(before[-1])
    rows = [r for d in sorted(keep) for r in hc.execute("SELECT day, id, platform, minutes FROM snapshots WHERE day = ?", (d,))]
    hc.close()
    out["tables"]["snapshots"] = {"cols": ["day", "id", "platform", "minutes"], "rows": rows}
    # Selbst eingetragene Spieldauern (der Rest kommt jederzeit neu von IGDB)
    ac = app_con()
    hltb.init_db(ac)
    cur = ac.execute("SELECT * FROM hltb WHERE manual = 1")
    out["tables"]["hltb"] = {"cols": [d[0] for d in cur.description], "rows": cur.fetchall()}
    ac.close()
    return out


def restore_backup(b):
    n = 0
    con = console_con()
    tracker.init_db(con)
    hc = history_con()
    for t, d in (b.get("tables") or {}).items():
        if t not in BACKUP_TABLES + ("snapshots", "hltb"):
            continue
        if t == "hltb":
            hltb.init_db(con)
        target = hc if t == "snapshots" else con
        have = {r[1] for r in target.execute(f"PRAGMA table_info({t})")}
        cols = [c for c in d.get("cols", []) if c in have]
        idx = [d["cols"].index(c) for c in cols]
        if t == "status_log":  # ohne Schlüssel: Doppelte vermeiden
            seen = set(target.execute("SELECT key, status, at FROM status_log").fetchall())
            rows = [r for r in d.get("rows", []) if tuple(r[:3]) not in seen]
        else:
            rows = d.get("rows", [])
        q = f"INSERT OR IGNORE INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        for r in rows:
            n += target.execute(q, [r[i] for i in idx]).rowcount
    con.commit(); hc.commit(); con.close(); hc.close()
    return n


def cloud_sync(force=False):
    cfg = load_config()
    if not cloud_ready(cfg) or not cloud_lock.acquire(blocking=False):
        return
    try:
        gh = cloud.GitHub(cfg["gh_repo"], cfg["gh_token"])
        gh.info()
        deletes, applied = [], 0
        for item in gh.list_edits():
            try:
                applied += apply_remote_edits(cloud.decrypt(gh.read_file(item), cfg["cloud_pass"]))
            except ValueError:
                print("  Cloud: Änderung vom Handy mit falschem Passwort verworfen")
            deletes.append(item["path"])
        snap = build_snapshot(cfg, gh)
        digest = hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()
        files = {}
        if digest != cloud_state.get("data_hash") or force:
            snap["pushed"] = now_iso()
            files["data.enc"] = cloud.encrypt(snap, cfg["cloud_pass"])
        today = dt.date.today().isoformat()
        if cloud_state.get("backup_day") != today:
            files["backup.enc"] = cloud.encrypt(build_backup(), cfg["cloud_pass"])
        app, app_hash = app_files()
        if app_hash != cloud_state.get("app_hash"):
            files.update(app)
        pub_release = None
        if cfg.get("publish_updates"):  # Updates für weitergegebene Installationen (nur Programmdateien, keine Daten/Keys)
            pf = updater.program_files(BASE)
            man = updater.manifest(pf, VERSION)
            pub_release = man["release"]
            if pub_release != cloud_state.get("pub_release"):
                files.update({"app/" + rel: b for rel, b in pf.items()})
                files["app/version.json"] = json.dumps(man, indent=1).encode()
        if cfg.get("partner_code"):
            try:
                pdata = cloud.encrypt(partner_summary(cfg), cfg["partner_code"])
                ph = hashlib.sha256(json.dumps(partner_summary(cfg, stable=True), sort_keys=True, default=str).encode()).hexdigest()
                if ph != cloud_state.get("partner_hash"):
                    files["partner.enc"] = pdata
                    cloud_state["partner_hash_new"] = ph
            except Exception as e:
                print("  Wir zwei:", err_text(e))
        if files or deletes:
            gh.commit(files, deletes, f"Gaming Recap {now_iso()}")
            print(f"  Cloud: hochgeladen ({', '.join(sorted(files)) or 'nur Aufräumen'})"
                  + (f", {applied} Handy-Änderungen übernommen" if applied else ""))
        if "backup.enc" in files:
            cloud_state["backup_day"] = today
        if pub_release:
            cloud_state["pub_release"] = pub_release
        if "partner.enc" in files:
            cloud_state["partner_hash"] = cloud_state.pop("partner_hash_new", None)
        cloud_state.update(last_push=now_iso(), error=None, data_hash=digest, app_hash=app_hash,
                           url=gh.pages_url(), pending=False, applied=applied)
    except Exception as e:
        cloud_state.update(error=err_text(e))
        print("  Cloud:", cloud_state["error"])
    finally:
        cloud_state["last_try"] = time.time()
        write_json(CLOUD_STATE_PATH, cloud_state)
        cloud_lock.release()


def scheduler():
    last_export, last_run, last_cloud, last_edits, last_health, started = None, 0, 0, 0, 0, time.time()
    while True:
        cfg = load_config()
        notify.PUSH["topic"] = (cfg.get("push_topic") or "").strip() or None
        hours = max(1, float(cfg.get("sync_hours") or 6))
        try:
            mtime = os.path.getmtime(cfg["playnite_export"])
        except OSError:
            mtime = None
        if time.time() - last_run > hours * 3600 or (mtime and mtime != last_export and time.time() - last_run > 900):
            last_export, last_run = mtime, time.time()
            run_sync()
        # Cloud: Änderungen sofort (max. alle 5 Min.), sonst alle 15 Min. nach Handy-Änderungen schauen
        try:
            check_notifications(cfg)
            if time.time() - last_health > 600 and time.time() - started > 300:
                last_health = time.time()
                check_health(cfg)
                check_expiry(cfg)
            offline_watchdog(cfg)
            if time.time() - started > 300:
                threading.Thread(target=update_worker, args=(cfg,), daemon=True).start()
            if cfg.get("partner_url") and time.time() - partner_state["last"] > 1800:
                threading.Thread(target=fetch_partner, args=(cfg,), daemon=True).start()
        except Exception as e:
            print("  Hinweise:", e)
        if cloud_ready(cfg) and not state["syncing"]:
            gap = time.time() - last_cloud
            has_edits = False
            if time.time() - last_edits > 120:  # Handy-Änderungen: alle 2 Minuten nachsehen (1 kleine Anfrage)
                last_edits = time.time()
                try:
                    has_edits = bool(cloud.GitHub(cfg["gh_repo"], cfg["gh_token"]).list_edits())
                except Exception:
                    pass
            if has_edits or (cloud_state.get("pending") and gap > 300) or gap > 900:
                last_cloud = time.time()
                cloud_sync()
        time.sleep(20)


# ================================================================ Playnite-Erweiterung

def ext_target():
    root = os.path.join(APPDATA, "Playnite")
    return os.path.join(root, "Extensions", "GamingRecapExport") if os.path.isdir(root) else None


def install_playnite_extension():
    dest = ext_target()
    if not dest:
        raise RuntimeError("Playnite wurde nicht gefunden. Installiere zuerst Playnite von playnite.link.")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(EXT_SRC, dest)
    return dest


def auto_update_extension():
    dest = ext_target()
    if not dest or not os.path.isdir(dest):
        return
    try:
        with open(os.path.join(dest, "extension.yaml"), encoding="utf-8") as a, \
                open(os.path.join(EXT_SRC, "extension.yaml"), encoding="utf-8") as b:
            if a.read() == b.read():
                return
        install_playnite_extension()
        print("  Playnite-Erweiterung aktualisiert - bitte Playnite einmal neu starten.")
    except OSError:
        pass


# ================================================================ Webserver

def public_config(cfg):
    out = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
    for k in SECRET_FIELDS:
        out[k + "_set"] = bool(str(cfg.get(k, "")).strip())
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "GamingRecap/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if self._local():
            return host in ("localhost", "127.0.0.1")
        return bool(load_config().get("lan")) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) is not None

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "Zugriff verweigert"})
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path in STATIC:
            fp = os.path.join(WEB_DIR, STATIC[path])
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    ctype = "application/manifest+json" if fp.endswith(".webmanifest") else mimetypes.guess_type(fp)[0]
                    return self._send(200, f.read(), ctype or "application/octet-stream")
        if path == "/api/data":
            period = urllib.parse.parse_qs(url.query).get("period", ["all"])[0]
            cache = load_cache()
            games = cache.get("games", [])
            pmins, info = period_minutes(games, period)
            cfg = load_config()
            sources = dict(cache.get("sources", {}))
            if cfg.get("hltb"):
                h = hltb.load(APP_DB)
                sources["Spieldauern (IGDB)"] = ({"ok": False, "error": hltb_state["error"]} if hltb_state["error"] else
                                                 {"ok": True, "count": len(h), "minutes": None,
                                                  "info": "optional: IGDB-Zugang in den Einstellungen eintragen" if hltb_state.get("missing") else
                                                  "lädt gerade …" if hltb_state["running"] else
                                                  (f"{hltb_state['left']} folgen" if hltb_state.get("left") else None)})
            if cloud_ready(cfg):
                sources["Handy-Cloud"] = ({"ok": False, "error": cloud_state["error"]} if cloud_state.get("error") else
                                          {"ok": True, "count": None, "minutes": None,
                                           "info": f"zuletzt {cloud_state['last_push'][:16].replace('T', ' ')} UTC"
                                           if cloud_state.get("last_push") else "noch nicht hochgeladen"})
            sources["Steam-Wertungen"] = ({"ok": False, "error": scores_state["error"]} if scores_state.get("error") else
                                          {"ok": True, "count": len(read_json(SCORES_PATH, {}).get("scores", {})), "minutes": None,
                                           "info": "Metascores laden gerade …"
                                           if scores_state["running"] else None})
            if cfg.get("steam_extras") and cfg["steam_api_key"].strip():
                sx = read_json(STEAMX_PATH, {})
                sources["Steam-Extras"] = ({"ok": False, "error": steamx_state["error"]} if steamx_state["error"] else
                                           {"ok": True, "count": None, "minutes": None,
                                            "info": "lädt gerade …" if steamx_state["running"] else
                                            f"{len(sx.get('ach', {}))} Spiele mit Erfolgen, {len(sx.get('wishlist', []))} auf der Wunschliste, "
                                            f"{len(sx.get('friends', []))} Freunde" + (f" · {sx['wishlist_error']}" if sx.get("wishlist_error") else "")})
            data = {"version": VERSION, "last_sync": cache.get("last_sync"), "syncing": state["syncing"],
                    "sources": sources, "period": info, "period_minutes": pmins, "games": games,
                    "playing": TRACKER.current, "can_edit": self._local(), "cloud_url": cloud_state.get("url"),
                    "configured": any(str(cfg[k]).strip() for k in SECRET_FIELDS[:3]) or os.path.exists(cfg["playnite_export"])}
            data.update(shared_data(cfg, games))
            return self._send(200, data)
        m = re.fullmatch(r"/api/img/([0-9a-fA-F-]{36})/(cover|background)", path)
        if m:
            imgs = PN_IMAGES.get(m.group(1)) or load_cache().get("pn_images", {}).get(m.group(1)) or {}
            fp = imgs.get(m.group(2))
            if fp and os.path.isfile(fp) and os.path.splitext(fp)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), mimetypes.guess_type(fp)[0] or "image/jpeg", cache=True)
            return self._send(404, {"error": "Kein Bild"})
        if path == "/api/report":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            return self._send(200, {"text": error_report(load_config())})
        if path == "/api/update":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            cfg = load_config()
            return self._send(200, {"version": VERSION, "release": updater.local_release(BASE, VERSION), "source": update_source(cfg),
                                    "publisher": bool(cfg.get("publish_updates")), "published": cloud_state.get("pub_release"),
                                    "auto": cfg.get("auto_update", True), **{k: v for k, v in update_state.items()}})
        if path == "/api/log":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            try:
                with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-250:]
            except OSError:
                lines = ["Noch kein Protokoll – Gaming Recap läuft mit sichtbarem Fenster."]
            return self._send(200, {"lines": [re.sub(r"(key|token|npsso|secret)=[^&\s]+", r"\1=***", l.rstrip()) for l in lines]})
        if path == "/api/status":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            cfg = load_config()
            hs = hltb_state
            tasks = [
                {"name": "Spieldauern & Cover (IGDB)", "running": hs["running"], "done": hs.get("done", 0), "total": hs.get("total", 0),
                 "error": hs.get("error"), "info": "kein Zugang eingetragen" if hs.get("missing") else
                 (f"{hs['left']} folgen beim nächsten Abgleich" if hs.get("left") else None)},
                {"name": "Steam-Erfolge, Wunschliste & Freunde", "running": steamx_state["running"], "error": steamx_state.get("error"),
                 "info": f"{steamx_state.get('ach', 0)} Erfolgs-Listen geladen" if steamx_state["running"] else None},
                {"name": "Steam-Wertungen & Metascore", "running": scores_state["running"], "done": scores_state.get("done", 0),
                 "total": scores_state.get("total", 0), "error": scores_state.get("error")},
                {"name": "Trophäen & Gratis-Spiele", "running": extras_state["running"], "error": extras_state.get("error")},
                {"name": "Preise, Cover & Metacritic", "running": media_state["running"], "done": media_state.get("done", 0),
                 "total": media_state.get("total", 0), "info": media_state.get("phase") if media_state["running"] else
                 None if (cfg.get("itad_key") or cfg.get("sgdb_key")) else "keine Keys eingetragen",
                 "error": media_state.get("error") or media_state.get("cover_error") or media_state.get("mc_error")},
                {"name": "Handy-Cloud", "running": cloud_lock.locked(), "error": cloud_state.get("error"),
                 "info": "lädt hoch …" if cloud_lock.locked() else None},
            ]
            return self._send(200, {"syncing": state["syncing"], "tasks": tasks})
        if path == "/api/covers/search":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            key = (load_config().get("sgdb_key") or "").strip()
            if not key:
                return self._send(400, {"error": "Kein SteamGridDB-Key eingetragen."})
            try:
                name, cands = media.sgdb_candidates(key, urllib.parse.parse_qs(url.query).get("q", [""])[0][:120], limit=16)
                return self._send(200, {"name": name, "covers": cands})
            except Exception as e:
                return self._send(500, {"error": err_text(e)})
        if path == "/api/config":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            cfg = public_config(load_config())
            dest = ext_target()
            af = autostart_file()
            cfg.update(autostart=bool(af and os.path.exists(af)), autostart_supported=bool(af))
            cfg.update(playnite_found=bool(dest), extension_installed=bool(dest and os.path.isdir(dest)),
                       cloud=cloud_state, tracker_supported=os.name == "nt")
            return self._send(200, cfg)
        self._send(404, {"error": "Nicht gefunden"})

    def _post_allowed(self):
        if not self._host_ok():
            return False
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return False
        return self.headers.get("Origin") in (None, f"http://{self.headers.get('Host') or ''}")

    def do_POST(self):
        if not self._post_allowed():
            return self._send(403, {"error": "Zugriff verweigert"})
        path = urllib.parse.urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length") or 0), 100_000)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._send(400, {"error": "Ungültige Daten"})

        if path == "/api/meta":
            try:
                return self._send(200, save_meta(body))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if path == "/api/backup/restore":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            cfg = load_config()
            if not cloud_ready(cfg):
                return self._send(400, {"error": "Handy-Cloud ist nicht eingerichtet."})
            try:
                gh = cloud.GitHub(cfg["gh_repo"], cfg["gh_token"])
                b = cloud.decrypt(gh.read_file({"path": "backup.enc"}), cfg["cloud_pass"])
                n = restore_backup(b)
                return self._send(200, {"restored": n, "at": b.get("at")})
            except FileNotFoundError:
                return self._send(404, {"error": "Noch kein Backup vorhanden."})
            except ValueError:
                return self._send(400, {"error": "Backup passt nicht zum Cloud-Passwort."})
            except Exception as e:
                return self._send(500, {"error": err_text(e)})
        if path == "/api/push/test":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            t = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("topic") or load_config().get("push_topic") or ""))
            if not t:
                return self._send(400, {"error": "Kein Push-Thema eingetragen."})
            ok = notify.push("Gaming Recap", "Test: Push-Nachrichten funktionieren 🎮", topic=t)
            return self._send(200 if ok else 500, {"ok": ok} if ok else {"error": "ntfy.sh nicht erreichbar"})
        if path == "/api/keys/export":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            pw = str(body.get("password") or "")
            if len(pw) < 8:
                return self._send(400, {"error": "Das Passwort braucht mindestens 8 Zeichen."})
            cfg = load_config()
            keys = {k: cfg.get(k) for k in SHARE_KEYS if str(cfg.get(k) or "").strip()}
            blob = cloud.encrypt({"type": "gaming-recap-keys", "keys": keys, "from": cfg.get("user_name") or ""}, pw)
            return self._send(200, {"file": blob.decode() if isinstance(blob, bytes) else blob, "count": len(keys)})
        if path == "/api/keys/import":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            try:
                d = cloud.decrypt(str(body.get("file") or "").strip(), str(body.get("password") or ""))
            except Exception:
                return self._send(400, {"error": "Datei oder Passwort passt nicht."})
            if not isinstance(d, dict) or d.get("type") != "gaming-recap-keys":
                return self._send(400, {"error": "Das ist keine Gaming-Recap-Keys-Datei."})
            cfg = load_config()
            got = []
            for k, v in (d.get("keys") or {}).items():
                if k in SHARE_KEYS and isinstance(v, str) and v.strip():
                    cfg[k] = v.strip()
                    got.append(k)
            write_json(CONFIG_PATH, cfg)
            sync_async()
            return self._send(200, {"imported": got})
        if path == "/api/update/check":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            threading.Thread(target=update_worker, args=(load_config(),), kwargs={"force": True, "apply_now": bool(body.get("apply"))}, daemon=True).start()
            return self._send(200, {"ok": True})
        if path in ("/api/restart", "/api/quit"):
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            restart_server(stop=path == "/api/quit")
            return self._send(200, {"ok": True})
        if path == "/api/autostart":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            try:
                return self._send(200, {"autostart": set_autostart(bool(body.get("on")))})
            except Exception as e:
                return self._send(500, {"error": err_text(e)})
        if path == "/api/kv":
            if body.get("key") not in KV_KEYS:
                return self._send(400, {"error": "Unbekannter Schlüssel"})
            val = clean_kv(body["key"], body.get("value"))
            if body["key"] == "wrapped":  # abgeschlossene Zeiträume bleiben, wie sie sind
                old = kv_get("wrapped", {}) or {}
                for k, a in old.items():
                    if a.get("final"):
                        val[k] = a
            kv_set(body["key"], val)
            cloud_state["pending"] = True
            return self._send(200, {"ok": True})
        if not self._local():
            return self._send(403, {"error": "Nur am PC"})
        if path == "/api/sync":
            sync_async()
            return self._send(200, {"ok": True})
        if path == "/api/hltb":
            v = body.get("main")
            hltb.set_manual(APP_DB, norm_title(body.get("key")),
                            int(float(v) * 60) if isinstance(v, (int, float)) and v > 0 else None)
            cloud_state["pending"] = True
            return self._send(200, {"ok": True})
        if path == "/api/cloud/push":
            threading.Thread(target=cloud_sync, kwargs={"force": True}, daemon=True).start()
            return self._send(200, {"ok": True})
        if path == "/api/playnite/install":
            try:
                return self._send(200, {"ok": True, "path": install_playnite_extension()})
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if path == "/api/config":
            cfg = load_config()
            for k in ("user_name", "partner_url", "update_url"):
                if isinstance(body.get(k), str):
                    cfg[k] = body[k].strip()[:200]
            for k in ("steam_id", "playnite_export", "gh_repo", "igdb_client_id"):
                if isinstance(body.get(k), str) and (body[k].strip() or k != "playnite_export"):
                    cfg[k] = body[k].strip()
            if isinstance(body.get("psn_npsso"), str) and body["psn_npsso"].strip() and body["psn_npsso"].strip() != cfg.get("psn_npsso"):
                cfg["psn_set_at"] = int(time.time())
            for k in SECRET_FIELDS:
                v = body.get(k)
                if isinstance(v, str) and v.strip():
                    if k == "cloud_pass" and len(v.strip()) < 10:
                        return self._send(400, {"error": "Das Cloud-Passwort muss mindestens 10 Zeichen haben."})
                    cfg[k] = v.strip()
                if body.get(k + "_clear"):
                    cfg[k] = ""
            if isinstance(body.get("push_topic"), str):
                t = re.sub(r"[^A-Za-z0-9_-]", "", body["push_topic"])[:64]
                cfg["push_topic"] = t
            for k in ("lan", "hltb", "tracker", "cloud_enabled", "steam_extras", "notify_sales", "notify_weekly",
                      "notify_free", "notify_release", "publish_updates", "auto_update", "prices_steam_only"):
                if isinstance(body.get(k), bool):
                    cfg[k] = body[k]
            for k, lo, hi in (("pause_hours", 0, 24), ("watts", 50, 2000), ("kwh_price", 0, 5)):
                if isinstance(body.get(k), (int, float)) and lo <= body[k] <= hi:
                    cfg[k] = body[k]
            if isinstance(body.get("backlog_stores"), list):
                cfg["backlog_stores"] = [str(x)[:40] for x in body["backlog_stores"]][:20]
            if isinstance(body.get("sync_hours"), (int, float)):
                cfg["sync_hours"] = max(1, min(48, body["sync_hours"]))
            write_json(CONFIG_PATH, cfg)
            cloud_state["pending"] = True
            partner_state["last"] = 0
            sync_async()
            return self._send(200, public_config(cfg))
        self._send(404, {"error": "Nicht gefunden"})


class Server(ThreadingHTTPServer):
    """Verhindert, dass unter Windows zwei Gaming-Recap-Instanzen denselben Port teilen."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        super().server_bind()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


LOG_PATH = os.path.join(DATA_DIR, "protokoll.txt")


def setup_logging():
    """Ohne Konsolenfenster (pythonw) landen alle Ausgaben in daten/protokoll.txt."""
    if sys.stdout is not None and "--log" not in sys.argv:
        return
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".alt")
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
        print(f"\n--- Start {time.strftime('%d.%m.%Y %H:%M:%S')} ---")
    except OSError:
        pass


def restart_server(stop=False, guard=False):
    """Startet Gaming Recap ohne Fenster neu (oder beendet es). Laufende Sessions setzt der Tracker danach fort."""
    def go():
        time.sleep(0.6)
        if not stop:
            exe = sys.executable
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.name == "nt" and os.path.exists(w):
                exe = w
            args = [a for a in sys.argv[1:] if a not in ("--no-browser", "--restarted")] + ["--no-browser", "--restarted"]
            kw = {"cwd": BASE, "close_fds": True}
            if os.name == "nt":
                kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED, NEW_GROUP, NO_WINDOW
            import subprocess
            script = [os.path.join(BASE, "guard.py")] if guard and os.path.exists(os.path.join(BASE, "guard.py")) \
                else [os.path.join(BASE, "server.py")] + args
            subprocess.Popen([exe] + script, **kw)
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def main():
    setup_logging()
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        write_json(CONFIG_PATH, DEFAULT_CONFIG)
    cfg = load_config()
    if (cfg.get("psn_npsso") or "").strip() and not cfg.get("psn_set_at"):
        cfg["psn_set_at"] = int(time.time())  # Ablauf-Erinnerung: ab jetzt mitzählen
        write_json(CONFIG_PATH, cfg)
    PN_IMAGES.update(load_cache().get("pn_images", {}))
    app_con().close()
    host = "0.0.0.0" if cfg.get("lan") else "127.0.0.1"
    httpd = None
    for attempt in range(40 if "--restarted" in sys.argv else 1):  # nach Neustart: warten, bis der alte Prozess weg ist
        try:
            httpd = Server((host, PORT), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if httpd is None:
        print(f"Port {PORT} ist belegt - läuft Gaming Recap schon? Öffne http://localhost:{PORT}")
        if "--no-browser" not in sys.argv:
            webbrowser.open(f"http://localhost:{PORT}")
        return
    auto_update_extension()
    TRACKER.set_targets(tracker_targets(load_cache().get("games", [])))
    print("=" * 60)
    print(f"  Gaming Recap {VERSION} läuft:  http://localhost:{PORT}")
    print(f"  Deine Daten liegen in:  {DATA_DIR}")
    if cfg.get("lan") and lan_ip():
        print(f"  Im WLAN (Handy):        http://{lan_ip()}:{PORT}")
    if cloud_state.get("url") and cloud_ready(cfg):
        print(f"  Handy-App (überall):    {cloud_state['url']}")
    print("  Läuft im Hintergrund. Beenden/Neustarten: Einstellungen › System." if sys.stdout is not sys.__stdout__ or sys.__stdout__ is None
          else "  Zum Beenden dieses Fenster schließen.")
    print("=" * 60)
    if "--no-sync" not in sys.argv:
        threading.Thread(target=scheduler, daemon=True).start()
        if cfg.get("tracker") and os.name == "nt":
            threading.Thread(target=TRACKER.run, daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
