#!/usr/bin/env python3
"""
Gaming Recap - lokaler Server (Etappe 2).

Quellen   : Playnite-Export + Steam-API (PC), PSN (PS5), OpenXBL (Xbox)
Tracker   : erkennt laufende PC-Spiele und speichert Sessions
HLTB      : Spieldauern von HowLongToBeat
Cloud     : verschlüsselte Kopie auf GitHub Pages fürs Handy
Nur Python-Standardbibliothek. Alles Persönliche bleibt auf diesem PC bzw. verschlüsselt.
"""
import base64
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
import common as pcutil  # noqa: E402  (PC-Helfer stecken in common.py)
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
    "partner_url": "", "partner_code": "", "allow_wake": False,
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
    if isinstance(e, KeyError):
        return f"Unerwartete Antwort der Plattform (Feld {e} fehlt)."
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
    return os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "GamingRecap.vbs")


def _pythonw():
    d = os.path.dirname(sys.executable or "")
    for n in ("pythonw.exe", "pyw.exe"):
        if os.path.exists(os.path.join(d, n)):
            return os.path.join(d, n)
    return "pythonw"


def other_autostart():
    """Selbst angelegter Autostart (Verknüpfung/Skript im Autostart-Ordner, das Gaming Recap startet)? Liefert den Dateinamen."""
    f = autostart_file()
    if not f:
        return None
    folder = os.path.dirname(f)
    needles = [x.lower() for x in (BASE, "server.py", "gamingrecap", "gaming recap", "start.bat")]
    try:
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if p == f or not os.path.isfile(p) or os.path.getsize(p) > 200000:
                continue
            raw = open(p, "rb").read()
            txt = (raw.decode("utf-8", "ignore") + raw.decode("utf-16-le", "ignore") + n).lower()
            if any(x in txt for x in needles):
                return n
    except OSError:
        pass
    return None


def set_autostart(on):
    """Startet mit Windows ganz ohne schwarzes Fenster (kleines VBScript ruft pythonw direkt auf)."""
    f = autostart_file()
    if not f:
        raise RuntimeError("Autostart gibt es nur unter Windows.")
    if on and other_autostart():   # schon selbst eingerichtet – kein zweiter Start
        return True
    old = f[:-4] + ".cmd"
    if os.path.exists(old):
        os.remove(old)
    if on:
        q = lambda x: x.replace('"', '""')
        vbs = ('Set sh = CreateObject("WScript.Shell")\r\n'
               'sh.CurrentDirectory = "%s"\r\n'
               'sh.Run """%s"" ""%s""", 0, False\r\n') % (q(BASE), q(_pythonw()), q(os.path.join(BASE, "server.py")))
        with open(f, "w", encoding="utf-16") as h:   # UTF-16: Umlaute im Benutzernamen gehen so nicht kaputt
            h.write(vbs)
    elif os.path.exists(f):
        os.remove(f)
    return bool(f and os.path.exists(f))


def windows_housekeeping():
    """Beim Start: alten Autostart (mit CMD-Fenster) umstellen und die „aus dem Internet“-Markierung von den
    Programmdateien entfernen – sonst fragt Windows bei jedem Start „Herausgeber konnte nicht verifiziert werden“."""
    if os.name != "nt":
        return
    try:
        f = autostart_file()
        if f and os.path.exists(f[:-4] + ".cmd"):
            set_autostart(True)
    except Exception as e:
        print("  Autostart umstellen:", e)
    n = 0
    for dp, dn, fn in os.walk(BASE):
        dn[:] = [d for d in dn if d not in ("daten", "__pycache__", ".git")]
        for x in fn:
            try:
                os.remove(os.path.join(dp, x) + ":Zone.Identifier")
                n += 1
            except OSError:
                pass
    if n:
        print(f"  Windows-Sicherheitsmarkierung von {n} Dateien entfernt")


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


PSN_EXPIRED = ("PlayStation-Anmeldung abgelaufen. Hol dir unter Einstellungen › Quellen einen neuen NPSSO-Code "
               "(dauert 1 Minute). Bis dahin bleiben deine PS5-Spiele mit dem letzten Stand erhalten.")
psn_lock = threading.Lock()


def _psn_token_request(fields):
    body = urllib.parse.urlencode(dict(fields, token_format="jwt")).encode()
    return http_json("https://ca.account.sony.com/api/authz/v3/oauth/token", data=body, method="POST",
                     headers={"Authorization": "Basic " + PSN_BASIC, "Content-Type": "application/x-www-form-urlencoded"})


def _psn_from_npsso(npsso):
    q = urllib.parse.urlencode({"access_type": "offline", "client_id": PSN_CLIENT, "redirect_uri": PSN_REDIRECT,
                                "response_type": "code", "scope": "psn:mobile.v2.core psn:clientapp"})
    req = urllib.request.Request("https://ca.account.sony.com/api/authz/v3/oauth/authorize?" + q,
                                 headers={"Cookie": f"npsso={npsso.strip()}", "User-Agent": UA})
    location = None
    try:
        location = urllib.request.build_opener(_NoRedirect).open(req, timeout=30).headers.get("Location")
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location")
    code = (urllib.parse.parse_qs(urllib.parse.urlparse(location or "").query).get("code") or [None])[0]
    if not code:   # z. B. ...?error=login_required&error_code=4165 – NPSSO ungültig
        raise RuntimeError(PSN_EXPIRED)
    return _psn_token_request({"code": code, "redirect_uri": PSN_REDIRECT, "grant_type": "authorization_code"})


def psn_access_token(npsso):
    """Zugriffstoken für die PlayStation-Schnittstelle. Nutzt das gespeicherte Refresh-Token (hält ca. 2 Monate,
    auch wenn du dich zwischendurch an der PS5 an- und abmeldest) und braucht den NPSSO nur, wenn das nicht mehr geht."""
    with psn_lock:
        path = os.path.join(DATA_DIR, "psn_token.json")
        t = read_json(path, {}) if os.path.exists(path) else {}
        now = time.time()
        nh = hashlib.sha256(npsso.strip().encode()).hexdigest()[:16]
        if t.get("npsso") != nh:      # neuer NPSSO eingetragen → frisch anmelden
            t = {"npsso": nh}
        if t.get("access") and t.get("access_exp", 0) > now + 120:
            return t["access"]
        j = None
        if t.get("refresh") and t.get("refresh_exp", 0) > now + 120:
            try:
                j = _psn_token_request({"refresh_token": t["refresh"], "grant_type": "refresh_token",
                                        "scope": "psn:mobile.v2.core psn:clientapp"})
            except Exception:
                j = None
        if j is None:
            j = _psn_from_npsso(npsso)
        t.update(access=j["access_token"], access_exp=now + int(j.get("expires_in") or 3000))
        if j.get("refresh_token"):
            t.update(refresh=j["refresh_token"], refresh_exp=now + int(j.get("refresh_token_expires_in") or 5000000))
        write_json(path, t)
        return t["access"]


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
    st = kv_get("snap_times", {}) or {}
    st[day] = now_iso()
    kv_set("snap_times", dict(sorted(st.items())[-120:]))


# ---------------------------------------------------------------- Konsolen-Sessions
# Konsolen liefern nur Gesamtzeit + "zuletzt gespielt". Wir vergleichen jeden Abgleich mit dem vorigen:
# Zuwachs = gespielt zwischen den beiden Abgleichen. Liegt nur ein Tag dazwischen, ist der Tag sicher;
# sonst landet ein Abend-Block auf dem letzten Spieltag und der Rest verteilt sich (markiert als "ungefähr").

def console_con():
    con = app_con()
    con.execute("CREATE TABLE IF NOT EXISTS console_sessions (game_key TEXT, device TEXT, start INTEGER, "
                "seconds INTEGER, approx INTEGER, PRIMARY KEY (game_key, device, start))")
    return con


STALE_H = 14 * 24     # Zuwachs ohne Spielen seit mehr als 14 Tagen = Nachmeldung der Plattform (zählt nicht)


def _local(ts):
    return dt.datetime.fromtimestamp(ts)


def _spread(start, end, minutes, cap=480, used=None, day_cap=960):
    """Verteilt Minuten auf die Kalendertage zwischen start und end (Unix-Zeit). „Zuletzt gespielt“ ist der
    sicherste Tag: von dort rückwärts bis 8 Std pro Tag auffüllen (nie mehr, als an dem Tag im Zeitfenster
    lag), einen Rest anteilig auf die freie Zeit der Tage."""
    end = max(end, start)
    days, d = [], _local(start).date()
    while d <= _local(end).date() and len(days) < 62:
        a = max(start, dt.datetime.combine(d, dt.time()).timestamp())
        b = min(end, dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).timestamp())
        room = max(1, int((b - a) / 60))
        if used is not None:   # alle Spiele zusammen höchstens 16 Std an einem Tag
            room = max(0, min(room, day_cap - used.get(d, 0)))
        days.append([d, room])
        d += dt.timedelta(days=1)
    out = {x: 0 for x, _ in days}
    left = minutes
    for x, room in reversed(days):
        take = min(left, cap, room)
        out[x] += take
        left -= take
        if left <= 0:
            break
    if left > 0:
        free = {x: max(0, room - out[x]) for x, room in days}
        tot = sum(free.values())
        for x, _ in days:
            add = int(left * free[x] / tot) if tot else int(left / len(days))
            out[x] += add
        out[days[-1][0]] += minutes - sum(out.values())
    res = {k: int(v) for k, v in out.items() if v > 0}
    if used is not None:
        for k, v in res.items():
            used[k] = used.get(k, 0) + v
    return res


def _src_of(gid):
    gid = str(gid)
    return "PlayStation" if gid.startswith("psn_") else "Xbox" if gid.startswith("xbox_") else "Playnite"


def reconcile_history(games, last_sync, today=None, src_ok=None):
    """Läuft bei jedem Abgleich VOR dem Speichern des heutigen Stands.
    War der PC aus, liefern PS5/Xbox die Spielzeit gesammelt nach. Wir verteilen sie anhand von
    „zuerst/zuletzt gespielt“ auf die echten Tage und tragen diese Tage im Verlauf nach.
    Steigt die Zeit, obwohl das Spiel lange nicht gestartet wurde (Nachmeldung), zählt sie nicht als gespielt.
    Kommt ein Spiel mit alter Spielzeit neu dazu (neuer Launcher, Import), ist das Altbestand."""
    today = today or dt.date.today()
    T = today.isoformat()
    con = history_con()
    prev_day = (con.execute("SELECT MAX(day) FROM snapshots WHERE day < ?", (T,)).fetchone() or [None])[0]
    if not prev_day:
        con.close()
        return 0
    prev = {i: (p, m) for i, p, m in con.execute("SELECT id, platform, minutes FROM snapshots WHERE day = ?", (prev_day,))}
    times = kv_get("snap_times", {}) or {}
    t_ref = parse_iso(times.get(prev_day)) or dt.datetime.combine(dt.date.fromisoformat(prev_day), dt.time(23, 59)).timestamp()
    if last_sync and (parse_iso(last_sync) or 0) > t_ref and (parse_iso(last_sync) or 0) < dt.datetime.combine(today, dt.time()).timestamp():
        t_ref = parse_iso(last_sync)
    now = min(time.time(), dt.datetime.combine(today + dt.timedelta(days=1), dt.time()).timestamp() - 60)
    prev_devs = {p for p, _ in prev.values()}
    plans, absorb, imports, sessions, fresh = {}, {}, {}, [], {}
    t_all = t_ref
    used = {}
    # Zurückgestellte „Nachmeldungen“: Endet danach eine Session (zuletzt gespielt wird neuer), war es echtes Spielen
    absorbed = kv_get("absorbed", {}) or {}
    revive, dirty_abs = [], False
    lp_now = {g["id"]: parse_iso(g.get("last_played")) for g in games}
    for gid in list(absorbed):
        keep = []
        for day_, gain_, t_abs in absorbed[gid]:
            lpn = lp_now.get(gid)
            if lpn and lpn >= t_abs - 6 * 3600 and day_ < T:
                revive.append((gid, day_, gain_))
                dirty_abs = True
            elif time.time() - t_abs < 3 * 86400:
                keep.append([day_, gain_, t_abs])
            else:
                dirty_abs = True
        if keep:
            absorbed[gid] = keep
        else:
            absorbed.pop(gid, None)
    src_day = kv_get("src_day", {}) or {}
    upto = kv_get("rec_upto", {}) or {}
    for g in games:
        gid, dev, m = g["id"], g.get("device"), int(g.get("minutes") or 0)
        p = prev.get(gid)
        lp = parse_iso(g.get("last_played"))
        fp = parse_iso(g.get("first_played")) or parse_iso(g.get("added"))
        # Stand der Quelle: Fiel z. B. PlayStation zuletzt aus, ist ihr gespeicherter Stand älter als der letzte Abgleich
        t_ref = t_all
        so = parse_iso((src_day.get(prev_day) or {}).get(_src_of(gid)))   # Stand der Quelle im gespeicherten Tag
        if so and so < t_ref:
            t_ref = so
        ru = upto.get(gid)
        if ru and ru > t_ref:     # bis hierhin schon nachgetragen
            t_ref = ru
        if p is None:
            if m <= 0:
                continue
            if dev not in prev_devs:   # Plattform zum ersten Mal erfasst: alles ist Ausgangsstand
                imports[gid] = (dev, m)
                continue
            genuine = (fp and fp >= t_ref - 3600) or (not fp and m <= IMPORT_MIN)
            if not genuine:
                imports[gid] = (dev, m)
                continue
            start, base, gain = max(t_ref, fp or t_ref), 0, m
            fresh[gid] = dev
        else:
            gain = m - p[1]
            if gain <= 0:
                continue
            if lp and lp < t_ref - STALE_H * 3600:
                absorb[gid] = gain        # Nachmeldung: nicht gespielt
                continue
            start, base = max(t_ref, fp or 0), p[1]   # nie vor dem ersten Spielen
        if dev == "PC" and (_local(start).date() >= today or not lp):
            continue                     # PC-Spiele ohne „zuletzt gespielt“: alles heute (der PC war ja an)
        end = min(now, max(lp or now, start))
        share = _spread(start, end, gain, used=used)
        if not share:
            continue
        if len(share) == 1 and min(share) >= today:
            continue                     # nur heute: nichts nachzutragen
        plans[gid] = (dev, base, share)
        upto[gid] = end
        if dev != "PC":
            k = norm_title(g["title"])
            for d, v in share.items():
                if v <= 0:
                    continue
                if d == _local(end).date():
                    sessions.append((k, dev, int(end - v * 60), int(v * 60), 1))
                else:
                    sessions.append((k, dev, int(dt.datetime.combine(d, dt.time(20, 0)).timestamp()), int(v * 60), 1))
    if not (plans or absorb or imports or fresh or revive):
        con.close()
        if dirty_abs:
            kv_set("absorbed", absorbed)
        return 0
    gap = []
    d = dt.date.fromisoformat(prev_day) + dt.timedelta(days=1)
    while d < today:
        gap.append(d.isoformat())
        d += dt.timedelta(days=1)
    with con:
        copied = {}
        if plans:
            # Alle Tage ab dem Tag vor dem frühesten Nachtrag bis gestern müssen als Stand existieren
            first_all = min(min(sh) for _, _, sh in plans.values()) - dt.timedelta(days=1)
            have = {r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots")}
            d = min(first_all, dt.date.fromisoformat(prev_day))
            src = max([x for x in have if x <= d.isoformat()] or [min(have)]) if d.isoformat() not in have else d.isoformat()
            while d < today:
                di = d.isoformat()
                if di in have:
                    src = di
                elif src:
                    con.execute("INSERT OR IGNORE INTO snapshots SELECT ?, id, platform, minutes FROM snapshots WHERE day = ?", (di, src))
                    have.add(di)
                    copied[di] = src
                d += dt.timedelta(days=1)
        for gid, (dev, base, share) in plans.items():
            first = min(share) - dt.timedelta(days=1)
            # Spiel in allen nötigen Tagen mit dem Ausgangswert führen (neue Spiele: 0)
            known = dict(con.execute("SELECT day, minutes FROM snapshots WHERE id = ?", (gid,)).fetchall())
            d, last = first, None
            for k_ in sorted(known):
                if k_ < first.isoformat():
                    last = known[k_]
            while d < today:
                di = d.isoformat()
                if di in known:
                    last = known[di]
                else:   # fehlender Tag: letzter bekannter Wert, sonst 0 (gab es da noch nicht)
                    con.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)", (di, gid, dev, last or 0))
                d += dt.timedelta(days=1)
            for dd, v in share.items():
                if dd < today and v:
                    con.execute("UPDATE snapshots SET minutes = minutes + ? WHERE id = ? AND day >= ? AND day < ?",
                                (v, gid, dd.isoformat(), T))
        for gid, gain in absorb.items():   # Nachmeldung: in der ganzen Vergangenheit mitführen → zählt nirgends als gespielt
            con.execute("UPDATE snapshots SET minutes = minutes + ? WHERE id = ? AND day < ?", (gain, gid, T))
            absorbed.setdefault(gid, []).append([T, gain, time.time()])
        for gid, day_, gain in revive:     # doch gespielt (Session lief noch): wieder als gespielt an dem Tag zählen
            con.execute("UPDATE snapshots SET minutes = minutes - ? WHERE id = ? AND day < ?", (gain, gid, day_))
        last_day = con.execute("SELECT MAX(day) FROM snapshots WHERE day < ?", (T,)).fetchone()[0] or prev_day
        for gid, dev in fresh.items():   # neues Spiel: am letzten Tag davor ausdrücklich mit 0 führen → alles danach zählt
            if gid not in plans:
                con.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, 0)", (last_day, gid, dev))
        if imports:   # Altbestand: in allen bisherigen Tagen mit voller Zeit führen → zählt nirgends als gespielt
            all_days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots WHERE day < ?", (T,))]
            for gid, (dev, m) in imports.items():
                con.executemany("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?)", [(gd, gid, dev, m) for gd in all_days])
    con.close()
    if absorb or dirty_abs:
        kv_set("absorbed", absorbed)
    if copied:   # kopierte Tage haben den Datenstand ihrer Vorlage
        st_, sd_ = kv_get("snap_times", {}) or {}, kv_get("src_day", {}) or {}
        for di, src in copied.items():
            if src in st_:
                st_[di] = st_[src]
            if src in sd_:
                sd_[di] = sd_[src]
        kv_set("snap_times", dict(sorted(st_.items())[-120:]))
        kv_set("src_day", dict(sorted(sd_.items())[-120:]))
    if plans:   # merken, bis wann je Spiel nachgetragen ist – ein zweiter Abgleich verteilt nichts erneut
        lim = time.time() - 120 * 86400
        kv_set("rec_upto", {k: v for k, v in upto.items() if v > lim})
    if sessions:
        cc = console_con()
        with cc:
            cc.executemany("INSERT OR REPLACE INTO console_sessions VALUES (?, ?, ?, ?, ?)", sessions)
        cc.close()
    out = []
    if plans:
        out.append(f"{len(plans)} Spiele auf {len({d for _, _, sh in plans.values() for d in sh})} Tage verteilt")
    if absorb:
        out.append(f"{len(absorb)} Nachmeldungen ignoriert")
    if imports:
        out.append(f"{len(imports)} Spiele als Altbestand")
    return ", ".join(out)


def repair_history_once(cur_games):
    """Einmalig: Nachmeldungen der Konsolen in den letzten 30 Tagen aus der Statistik nehmen (z. B. GTA V am 17.11.)."""
    if kv_get("repair_v1"):
        return 0
    games = {g["id"]: g for g in cur_games}
    con = history_con()
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")][-31:]
    fixed = []
    for a, b in zip(days, days[1:]):
        A = dict(con.execute("SELECT id, minutes FROM snapshots WHERE day = ?", (a,)).fetchall())
        for gid, m in con.execute("SELECT id, minutes FROM snapshots WHERE day = ?", (b,)).fetchall():
            g = games.get(gid)
            if not g or g.get("device") == "PC" or gid not in A or m <= A[gid]:
                continue
            lp = parse_iso(g.get("last_played"))
            if lp and lp < dt.datetime.combine(dt.date.fromisoformat(a), dt.time()).timestamp() - STALE_H * 3600:
                fixed.append((gid, m - A[gid], a, b, norm_title(g["title"])))
    with con:
        for gid, gain, a, b, k in fixed:
            con.execute("UPDATE snapshots SET minutes = minutes + ? WHERE id = ? AND day <= ?", (gain, gid, a))
    con.close()
    if fixed:
        cc = console_con()
        with cc:
            for gid, gain, a, b, k in fixed:
                s0 = dt.datetime.combine(dt.date.fromisoformat(b), dt.time()).timestamp()
                cc.execute("DELETE FROM console_sessions WHERE game_key = ? AND start >= ? AND start < ?", (k, s0, s0 + 86400))
        cc.close()
        print("  Nachmeldungen korrigiert:", ", ".join(f"{k} ({g} Min, {b})" for _, g, a, b, k in fixed))
    # Lücke vor dem letzten Stand (PC war aus, z. B. GTA VI vom 19.–21.11.): Konsolenzeit auf die Tage verteilen
    con = history_con()
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
    con.close()
    if len(days) >= 2:
        a, b = days[-2], days[-1]
        bd = dt.date.fromisoformat(b)
        if (bd - dt.date.fromisoformat(a)).days > 1 and (dt.date.today() - bd).days <= 4:
            con = history_con()
            rows = con.execute("SELECT id, platform, minutes FROM snapshots WHERE day = ?", (b,)).fetchall()
            con.close()
            gl = [dict(games.get(i, {"title": i}), id=i, device=p, minutes=m) for i, p, m in rows]
            r = reconcile_history(gl, None, today=bd)
            if r:
                print("  Lücke nachgetragen:", r)
    kv_set("repair_v1", now_iso())
    return len(fixed)


def load_console_sessions(since):
    con = console_con()
    rows = con.execute("SELECT game_key, start, seconds, device, approx FROM console_sessions WHERE start >= ? "
                       "ORDER BY start", (since,)).fetchall()
    con.close()
    return [list(r) for r in rows]


def first_seen_minutes(con):
    """Spielzeit beim ersten Auftauchen jedes Spiels im Verlauf. Kommt ein Spiel schon mit viel Spielzeit
    dazu (z. B. neuer Launcher in Playnite), ist das Altbestand und zählt nicht als neu gespielt."""
    return {i: m for i, m, _ in con.execute("SELECT id, minutes, MIN(day) FROM snapshots GROUP BY id")}


IMPORT_MIN = 600   # mehr als 10 Std beim ersten Auftauchen = Import


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
    first = first_seen_minutes(con)
    for g in games:
        if g["id"] in baseline:
            b = baseline[g["id"]]
        elif g["device"] in base_devices:
            fm = first.get(g["id"], 0)
            b = fm if fm > IMPORT_MIN else 0
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


REMOTE_CMDS = ("after_session", "at_time", "cancel", "sleep", "shutdown", "wake_partner")
KV_KEYS = {"goals", "year_overrides", "wrapped", "notpc", "snotes", "covers", "paid", "notify", "critic", "ui", "pet", "plans", "plan_resp", "remote", "plan_pushed",
           "advent", "wishes", "wish_done", "surprises", "capsule", "pins", "features", "dates", "fx", "seen_new", "visits", "pairs"}
FEATURES = ("advent", "moment", "greetpet", "night", "news", "moments", "chronik", "capsule", "wishes", "surprise", "home", "season")
WISH_CATS = ("spiel", "erleben", "essen", "ausflug", "sonst")


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


NOTIFY_CATS = ("sales", "lows", "free", "release", "weekly", "monthly", "yearly", "pause", "health", "offline", "duo", "pet")


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


def quiet_end(prefs, now=None):
    """Zeitpunkt (Unix), an dem die Nachtruhe endet."""
    q = prefs.get("quiet")
    if not q:
        return None
    now = now or dt.datetime.now()
    t = now.replace(hour=q[1], minute=0, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return int(t.timestamp()) + 60


def FEATURE_ON(k):
    return (kv_get("features", {}) or {}).get(k, True) is not False


def fest_of(d, dates, pname=None):
    """Fest an diesem Tag → (Titel, Text) für eine Push-Nachricht, sonst None."""
    md = d.strftime("%m-%d")
    if dates.get("me") == md:
        return ("🎂 Alles Gute zum Geburtstag!", "Dein Begleiter hat heute eine Party-Mütze auf – schau mal rein.")
    if pname and dates.get("pa") == md:
        return (f"🎂 Heute hat {pname} Geburtstag", "Vergiss den Gruß nicht – in der App steigen heute Luftballons.")
    an = str(dates.get("anniv") or "")
    if an[5:] == md and an[:4] < str(d.year):
        return ("💞 Heute ist euer Jahrestag", f"{d.year - int(an[:4])} Jahre – in eurer App ist heute alles voller Herzen.")
    if md == "12-01" and FEATURE_ON("advent"):
        return ("🎄 Der Adventskalender ist offen", "Heute beginnt eure Weihnachtsgeschichte – mit einem verschwundenen Stern.")
    if md == "12-24":
        return ("🎄 Frohe Weihnachten", "Das letzte Kapitel und die Weihnachtsstube warten auf euch.")
    if md == "12-31":
        return ("🎆 Silvester", "Um Mitternacht gibt es Feuerwerk in der App – und die Silvester-Brille für deinen Begleiter.")
    if md == "02-14":
        return ("💘 Happy Valentinstag", "Heute schweben Herzen durch eure App.")
    if d == _easter(d.year):
        return ("🐣 Frohe Ostern", "In der App sind heute Eier versteckt – findest du sie?")
    if md == "10-31":
        return ("🎃 Halloween", "Fledermäuse in der App und ein Hexenhut für deinen Begleiter.")
    return None


def plan_ahead(cfg, now=None):
    """Plant Benachrichtigungen bis 3 Tage im Voraus bei ntfy ein. So kommen sie auch, wenn der PC aus ist.
    Läuft stündlich und aktualisiert/löscht geplante Nachrichten, wenn sich etwas ändert."""
    now = now or dt.datetime.now()
    prefs = notify_prefs(cfg)
    planned = kv_get("planned", {}) or {}
    topic = (cfg.get("push_topic") or "").strip()
    cats = prefs.get("cats") or {}
    want = {}
    if topic and prefs.get("phone", True):
        base = cloud_state.get("url") or f"http://localhost:{PORT}/"
        def at_(d, h):
            t = dt.datetime.combine(d, dt.time(h, 0))
            if in_quiet(prefs, t):
                t = dt.datetime.fromtimestamp(quiet_end(prefs, t))
            return t
        def add(key, kind, t, title, body, url=None, suffix=""):
            if cats.get(kind, True) and now + dt.timedelta(minutes=2) < t < now + dt.timedelta(hours=70):
                want["gr-" + key] = [int(t.timestamp()), title, body, url, suffix]
        stamp = now.strftime("%a %H:%M").replace("Mon", "Mo").replace("Tue", "Di").replace("Wed", "Mi").replace("Thu", "Do").replace("Fri", "Fr").replace("Sat", "Sa").replace("Sun", "So")
        try:
            st = pet_state(cfg)
        except Exception:
            st = {}
        today = now.date()
        sun = today + dt.timedelta(days=(6 - today.weekday()) % 7)
        wk = (sun - dt.timedelta(days=6)).isoformat()
        p = next((x for x in st.get("pets") or [] if x["id"] == st.get("active")), None)
        if p and p["stage"] and kv_get("pet_weekly_sent") != wk:
            body = pet_weekly_text(st, p)
            if body:
                add(f"petweek-{wk}", "pet", at_(sun, 19), f"🐾 Wochenrückblick von {p['nick'] or p['stage_name']}", body, base + "#/begleiter", f" (Stand {stamp})")
        wb = (st.get("current") or {}).get("week")
        if wb and not wb.get("won"):
            left = max(0, wb["hp"] - wb["dmg"])
            add(f"boss-{wk}", "pet", at_(sun, 12), f"⚔️ {wb['name']} flieht heute um Mitternacht",
                f"Noch {left} Leben – etwa {max(1, round(left / 60))} Std Spielen reichen.", base + "#/begleiter")
        open_q = [q for q in st.get("quests") or [] if not q["done"]]
        if open_q:
            add(f"quest-{wk}", "pet", at_(sun - dt.timedelta(days=1), 11), f"📋 Noch {len(open_q)} Wochenaufgabe{'n' if len(open_q) > 1 else ''} offen",
                " · ".join(q["t"] for q in open_q[:3]) + " – bis Sonntag Zeit.", base + "#/begleiter")
        # Zockabende: 1 Std vorher erinnern (eigene mit Zusage des Partners und zugesagte des Partners)
        pa_ = kv_get("partner", None) or {}
        pname = pa_.get("name") or "deinem Partner"
        mine_resp, their_resp = kv_get("plan_resp", {}) or {}, pa_.get("plan_resp") or {}
        cand = [(p_, their_resp.get(p_["id"])) for p_ in kv_get("plans", []) or []] + [(p_, mine_resp.get(p_["id"])) for p_ in pa_.get("plans") or []]
        for p_, r_ in cand:
            if p_.get("cancel") or r_ != "yes":
                continue
            try:
                t = dt.datetime.fromisoformat(p_["at"]) - dt.timedelta(hours=1)
            except ValueError:
                continue
            add(f"plan-{p_['id']}", "duo", t, f"🎮 In einer Stunde: Zockabend mit {pname}",
                (p_.get("game") or "Spiel steht noch nicht fest") + (f" – {p_['note']}" if p_.get("note") else ""), base + "#/freunde")
        # Feste & Advent (höchstens eine pro Tag, einzeln abschaltbar unter „Feste & Effekte“)
        fx_push = ((kv_get("fx", {}) or {}).get("push")) or {}
        for d_ in (today + dt.timedelta(days=i) for i in range(3)):
            fest = fest_of(d_, kv_get("dates", {}) or {}, pname if pa_ else None)
            if fest and fx_push.get("fest", True):
                add(f"fest-{d_.isoformat()}", "duo", at_(d_, 10), fest[0], fest[1], base + "#/uebersicht")
            elif d_.month == 12 and d_.day in (5, 9, 14, 19) and fx_push.get("vote", True) and FEATURE_ON("advent"):
                add(f"vote-{d_.isoformat()}", "duo", at_(d_, 17), "🗳️ Abstimmung in eurer Weihnachtsgeschichte",
                    "Wie geht es für eure Begleiter weiter? Heute entscheidet ihr zusammen.", base + "#/freunde")
        # Neuerscheinungen von der Wunschliste
        sx = read_json(STEAMX_PATH, {}) or {}
        for w in sx.get("wishlist") or []:
            pr = (sx.get("prices") or {}).get(str(w.get("appid"))) or {}
            rel = pr.get("release")
            if rel:
                d = dt.date.fromtimestamp(rel)
                if today < d:
                    add(f"rel-{w.get('appid')}", "release", at_(d, 10), "Heute erschienen",
                        f"{pr.get('name') or 'Ein Spiel von deiner Wunschliste'} ist ab heute erhältlich.", f"https://store.steampowered.com/app/{w.get('appid')}")
        # Monats- und Jahres-Wrapped
        first = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        mnames = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
        last = first - dt.timedelta(days=1)
        add(f"month-{last.strftime('%Y-%m')}", "monthly", at_(first, 10), f"🎬 Dein {mnames[last.month - 1]}-Wrapped ist da",
            "Tippe für dein Monats-Wrapped.", f"{base}#/wrapped/arch:{last.strftime('%Y-%m')}")
        if first.month == 12:
            add(f"year-{first.year}", "yearly", at_(first, 10), f"🎬 Dein Gaming-Jahr {first.year} ist da", "Dein Jahres-Wrapped ist fertig – tippe für deine Story.", f"{base}#/wrapped/year")
        if first.month == 1:
            add(f"yearfinal-{first.year - 1}", "yearly", at_(first, 10), f"🏁 {first.year - 1} ist abgeschlossen",
                f"Dein komplettes Gaming-Jahr {first.year - 1} steht fest. Tippe für das finale Wrapped.", f"{base}#/wrapped/arch:{first.year - 1}")
    nowts = now.timestamp()
    changed = False
    for sid, (when, *_r) in list(planned.items()):
        if when > nowts and sid not in want:      # nicht mehr nötig (z. B. Boss besiegt) → absagen
            notify.cancel(sid, topic)
            planned.pop(sid)
            changed = True
        elif when < nowts - 8 * 86400:
            planned.pop(sid)
            changed = True
    for sid, (when, title, body, url, suffix) in want.items():
        h = hashlib.sha256(f"{when}|{title}|{body}|{url}".encode()).hexdigest()[:10]   # „Stand“-Zeit zählt nicht als Änderung
        old = planned.get(sid)
        if old and old[1] == h:
            continue
        if old and old[0] > nowts:
            notify.cancel(sid, topic)
        if notify.push(title, body + suffix, url, delay=when, sid=sid):
            planned[sid] = [when, h]
            changed = True
    if changed:
        kv_set("planned", planned)
    return want


def planned_done(sid, now=None):
    """Wurde diese Nachricht schon vorab bei ntfy eingeplant und ist ihr Zeitpunkt erreicht? Dann nicht doppelt senden."""
    x = (kv_get("planned", {}) or {}).get(sid)
    return bool(x) and x[0] <= (now or time.time()) + 3600


def alert(kind, title, body, url=None, queue=True):
    """Zentrale Stelle für alle Hinweise: Thema an/aus, Windows und/oder Handy, Nachtruhe fürs Handy."""
    prefs = notify_prefs()
    if not (prefs.get("cats") or {}).get(kind, True):
        return False
    phone = bool(prefs.get("phone", True))
    if kind in ("free", "sales") and cloud_notify_active():
        phone = False      # kommt schon von der GitHub Action aufs Handy
    if phone and in_quiet(prefs) and not queue:
        phone = False
    if phone and in_quiet(prefs):
        end = quiet_end(prefs)
        if end and notify.push(title, body, url, delay=end):   # ntfy stellt es nach der Nachtruhe zu – auch bei ausgeschaltetem PC
            phone = False
        else:
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
    if key == "pet":  # Begleiter: Eier/Begleiter, wer aktiv ist, Accessoires, Lebensraum
        v = v if isinstance(v, dict) else {}
        pets = []
        for p in (v.get("pets") or [])[:12]:
            if isinstance(p, dict) and p.get("sp") in PET_SPECIES and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(p.get("start") or "")):
                pets.append({"id": re.sub(r"[^a-z0-9]", "", str(p.get("id") or ""))[:12] or f"p{len(pets) + 1}", "sp": p["sp"],
                             "nick": str(p.get("nick") or "")[:24], "start": p["start"]})
        ids = {p["id"] for p in pets}
        act = []
        for a in (v.get("act") or [])[-300:]:
            if isinstance(a, list) and len(a) in (2, 3) and a[1] in ids and re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[0-9:.]+(Z|[+-]\d{2}:?\d{2})?)?", str(a[0])):
                act.append([str(a[0])[:32], a[1]] + ([int(a[2])] if len(a) == 3 and isinstance(a[2], (int, float)) else []))
        acc = {k: str(x)[:20] for k, x in (v.get("acc") or {}).items() if k in ids and x}
        acc2 = {k: str(x)[:20] for k, x in (v.get("acc2") or {}).items() if k in ids and x}
        _pet_cache["v"] = None
        gifts = [{"acc": str(g.get("acc"))[:20], "at": str(g.get("at") or "")[:10]} for g in (v.get("gifts") or [])[-50:]
                 if isinstance(g, dict) and g.get("acc") in PET_ACC]
        out = {"pets": pets, "act": act, "acc": acc, "acc2": acc2, "hab": re.sub(r"[^a-z]", "", str(v.get("hab") or ""))[:20], "gifts": gifts}
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v.get("epoch") or "")):
            out["epoch"] = v["epoch"]
            out["base"] = {k: int(x) for k, x in (v.get("base") or {}).items() if k in ids and isinstance(x, (int, float))}
        return out
    if key == "plans":  # Zockabend-Vorschläge: [{id, at, game, note, created}]
        out = []
        for x in (v if isinstance(v, list) else [])[-40:]:
            if isinstance(x, dict) and re.fullmatch(r"[a-z0-9]{4,16}", str(x.get("id") or "")) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", str(x.get("at") or "")[:16]):
                out.append({"id": x["id"], "at": str(x["at"])[:16], "game": str(x.get("game") or "")[:80], "note": str(x.get("note") or "")[:140],
                            "created": int(x.get("created") or 0), "cancel": bool(x.get("cancel"))})
        return out
    if key == "plan_resp":  # Antworten auf Vorschläge des Partners: {id: "yes"|"no"|"maybe"}
        return {str(k)[:16]: x for k, x in (v if isinstance(v, dict) else {}).items() if x in ("yes", "no", "maybe")}
    if key == "dates":  # Geburtstage + Jahrestag: {"me": "06-24", "pa": "03-13", "anniv": "2023-05-01"}
        v = v if isinstance(v, dict) else {}
        out = {k: str(v[k]) for k in ("me", "pa") if re.fullmatch(r"\d\d-\d\d", str(v.get(k) or ""))}
        if re.fullmatch(r"\d{4}-\d\d-\d\d", str(v.get("anniv") or "")): out["anniv"] = str(v["anniv"])
        return out
    if key == "fx":  # Effekte, Töne, Modus, Benachrichtigungen
        v = v if isinstance(v, dict) else {}
        out = {k: bool(v.get(k)) for k in ("noeffects", "nosound", "light") if k in v}
        out["push"] = {k: bool(x) for k, x in (v.get("push") or {}).items() if k in ("fest", "vote", "answers", "yeti", "visit")} if isinstance(v.get("push"), dict) else {}
        return out
    if key == "pairs":  # Begleiter-Paare: {"pid|sp|name": "2026-11-22"} (seit wann sie sich kennen)
        return {str(k)[:60]: str(x)[:10] for k, x in list((v if isinstance(v, dict) else {}).items())[-40:] if re.fullmatch(r"\d{4}-\d\d-\d\d", str(x))}
    if key == "seen_new":  # „Was ist neu“ weggeklickt: Versionskennung
        return str(v or "")[:40]
    if key == "visits":  # Besuche/Geschenke der Begleiter: [{id, at, kind, item, auto}]
        out = []
        for x in (v if isinstance(v, list) else [])[-60:]:
            if isinstance(x, dict) and re.fullmatch(r"[a-z0-9]{4,16}", str(x.get("id") or "")):
                out.append({"id": x["id"], "at": int(x.get("at") or 0), "kind": x.get("kind") if x.get("kind") in ("visit", "gift", "card") else "visit",
                            "item": re.sub(r"[^a-z_]", "", str(x.get("item") or ""))[:24], "auto": bool(x.get("auto"))})
        return out
    if key == "advent":  # geöffnete Türchen: {"o": {"2026-12-05": unix}}
        o = (v or {}).get("o") if isinstance(v, dict) else None
        day = lambda k: re.fullmatch(r"\d{4}-12-(0[1-9]|1\d|2[0-4])", str(k))
        a = v.get("a") if isinstance(v, dict) and isinstance(v.get("a"), dict) else {}
        vo = v.get("v") if isinstance(v, dict) and isinstance(v.get("v"), dict) else {}
        return {"o": {str(k): int(x) for k, x in list((o if isinstance(o, dict) else {}).items())[-60:]
                      if day(k) and isinstance(x, (int, float))},
                "a": {str(k): str(x).strip()[:400] for k, x in list(a.items())[-40:] if day(k) and str(x).strip()},
                "v": {str(k): int(x) for k, x in list(vo.items())[-20:] if day(k) and x in (1, 2)}}
    if key == "wishes":  # Wunschbaum: [{id, t, cat, at, done}]
        out = []
        for x in (v if isinstance(v, list) else [])[-100:]:
            if isinstance(x, dict) and re.fullmatch(r"[a-z0-9]{4,16}", str(x.get("id") or "")) and str(x.get("t") or "").strip():
                out.append({"id": x["id"], "t": str(x["t"]).strip()[:80], "cat": x.get("cat") if x.get("cat") in WISH_CATS else "sonst",
                            "at": int(x.get("at") or 0), "done": int(x.get("done") or 0), "note": str(x.get("note") or "")[:140]})
        return out
    if key == "wish_done":  # Wünsche des Partners, die ich abgehakt habe: {id: unix}
        return {str(k)[:16]: int(x) for k, x in list((v if isinstance(v, dict) else {}).items())[-200:]
                if re.fullmatch(r"[a-z0-9]{4,16}", str(k)) and isinstance(x, (int, float))}
    if key == "surprises":  # Überraschungs-Umschläge: [{id, title, text, reveal, at}]
        out = []
        for x in (v if isinstance(v, list) else [])[-20:]:
            if isinstance(x, dict) and re.fullmatch(r"[a-z0-9]{4,16}", str(x.get("id") or "")) \
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", str(x.get("reveal") or "")[:16]):
                out.append({"id": x["id"], "title": str(x.get("title") or "")[:60], "text": str(x.get("text") or "")[:400],
                            "reveal": str(x["reveal"])[:16], "at": int(x.get("at") or 0)})
        return out
    if key == "capsule":  # Zeitkapsel: {text, at, open: YYYY-MM-DD}
        v = v if isinstance(v, dict) else {}
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v.get("open") or "")) or not str(v.get("text") or "").strip():
            return {}
        return {"text": str(v["text"])[:1500], "at": int(v.get("at") or 0), "open": v["open"]}
    if key == "pins":  # angepinnte Erinnerungen
        return [str(x)[:60] for x in (v if isinstance(v, list) else [])[-200:] if isinstance(x, str)]
    if key == "features":  # einzeln abschaltbare neue Funktionen
        return {k: bool(x) for k, x in (v if isinstance(v, dict) else {}).items() if k in FEATURES}
    if key == "plan_pushed":  # welche Zockabend-Nachrichten schon direkt per Push raus sind (vom Handy oder PC)
        return {str(k)[:40]: int(x or 0) for k, x in list((v if isinstance(v, dict) else {}).items())[-300:]
                if re.fullmatch(r"[pcr]:[a-z0-9]{4,16}(:(yes|no|maybe))?", str(k))}
    if key == "remote":  # Befehl vom Handy an den eigenen PC
        v = v if isinstance(v, dict) else {}
        if v.get("cmd") not in REMOTE_CMDS:
            return {}
        return {"id": re.sub(r"[^a-z0-9]", "", str(v.get("id") or ""))[:16], "cmd": v["cmd"], "arg": str(v.get("arg") or "")[:8], "ts": int(v.get("ts") or 0)}
    if key == "ui":  # Design (Farbwelt), gilt für PC und Handy
        v = v if isinstance(v, dict) else {}
        return {"theme": re.sub(r"[^a-z0-9-]", "", str(v.get("theme") or ""))[:20]}
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


def _local_ts(ts):
    """ISO-Zeit (auch mit Z/Offset) → lokale Zeit als 'YYYY-MM-DDTHH:MM:SS'."""
    t = parse_iso(ts) if len(ts) > 10 else None
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S") if t else ts[:10]


def pet_today_minutes():
    """Heute schon gespielte Minuten (Stand letzter Abgleich + laufende PC-Session) – Teilungsstelle bei Wechseln."""
    try:
        m = _pet_days(dt.date.today()).get(dt.date.today().isoformat(), {}).get("min", 0)
    except Exception:
        m = 0
    for c in TRACKER.current or []:
        try:
            m += max(0, int((time.time() - (parse_iso(c.get("since")) or time.time())) / 60))
        except Exception:
            pass
    return m


def merge_pet(new, now_minutes=None):
    """Begleiter-Änderung von PC oder Handy einarbeiten, statt alles zu überschreiben:
    keine doppelten Eier, Wechsel werden gesammelt (mit Teilungsstelle), Neustart-Epoche bleibt."""
    old = clean_kv("pet", kv_get("pet", {}) or {})
    new = clean_kv("pet", new)
    T = dt.date.today().isoformat()
    ep = old.get("epoch")
    pets = [dict(p) for p in old.get("pets") or []]
    byid = {p["id"]: p for p in pets}
    owned = {p["sp"] for p in pets}
    try:
        ready = set(pet_state(load_config(), force=True).get("eggs_ready") or []) if pets else set()
    except Exception:
        ready = set()
    for p in new.get("pets") or []:
        if p["id"] in byid:
            byid[p["id"]]["nick"] = p.get("nick") or ""
            continue
        ok = (not pets and PET_SPECIES[p["sp"]]["start"]) or p["sp"] in ready
        if ok and p["sp"] not in owned:
            q = dict(p, start=max(p["start"], ep or p["start"], T if pets else p["start"]))
            pets.append(q)
            byid[q["id"]] = q
            owned.add(q["sp"])
    ids = set(byid)
    have = {(a[0], a[1]) for a in old.get("act") or []}
    act = [a for a in old.get("act") or [] if a[1] in ids]
    nm = pet_today_minutes() if now_minutes is None else now_minutes
    for a in new.get("act") or []:
        if (a[0], a[1]) in have or a[1] not in ids:
            continue
        ts = _local_ts(a[0])
        if (ts, a[1]) in have or (ep and ts[:10] < ep):
            continue
        # Wechsel gilt ab dem Moment, in dem der PC davon erfährt – so verliert nie ein Begleiter schon verdiente Erfahrung
        now_ts = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if len(ts) == 10 or ts < now_ts:
            ts = now_ts
        act.append([ts, a[1], nm])
        have.add((ts, a[1]))
    out = {"pets": pets, "act": act[-300:], "acc": {k: x for k, x in (new.get("acc") or {}).items() if k in ids},
           "acc2": {k: x for k, x in (new.get("acc2") or {}).items() if k in ids},
           "hab": new.get("hab") or old.get("hab") or "lichtung"}
    gk = {(g["acc"], g["at"]) for g in old.get("gifts") or []}
    out["gifts"] = (old.get("gifts") or []) + [g for g in new.get("gifts") or [] if (g["acc"], g["at"]) not in gk]
    if ep:
        out["epoch"], out["base"] = ep, old.get("base") or {}
    _pet_cache["v"] = None
    return out


def pet_soft_reset():
    """Einmalig: Begleiter-Spiel neu starten. Nur das erste Tier bleibt (geschlüpft, Level 5); Statistik bleibt unberührt."""
    if kv_get("pet_reset_v1"):
        return False
    raw = kv_get("pet", {}) or {}
    pets = raw.get("pets") or []
    kv_set("pet_reset_v1", now_iso())
    if not pets:
        return False
    T = dt.date.today().isoformat()
    first = dict(pets[0], start=T)
    kv_set("pet", {"pets": [first], "act": [[T, first["id"]]], "acc": {}, "hab": "lichtung", "gifts": [],
                   "epoch": T, "base": {first["id"]: pet_xp_for(5)}})
    kv_set("pet_seen", None)
    _pet_cache["v"] = None
    print(f"  Begleiter-Spiel neu gestartet: {PET_SPECIES.get(first['sp'], {}).get('name', first['sp'])} bleibt mit Level 5")
    return True


def self_check(cfg=None):
    """Prüft die echten Daten auf Dinge, die nie passieren dürfen. Liefert [(ok, Bereich, Text)]."""
    cfg = cfg or load_config()
    out = []
    def add(ok, area, text):
        out.append({"ok": bool(ok), "area": area, "text": text})
    # Verlauf
    con = history_con()
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")][-60:]
    drops, big = [], []
    for a, b in zip(days, days[1:]):
        A = dict(con.execute("SELECT id, minutes FROM snapshots WHERE day = ?", (a,)).fetchall())
        B = dict(con.execute("SELECT id, minutes FROM snapshots WHERE day = ?", (b,)).fetchall())
        drops += [(b, k) for k, m in B.items() if k in A and m < A[k] - 5]
        gain = sum(max(0, m - A.get(k, m)) for k, m in B.items())
        if gain > 20 * 60:
            big.append((b, gain // 60))
    missing = [d for d in days if d >= (dt.date.today() - dt.timedelta(days=14)).isoformat()]
    con.close()
    add(not drops, "Verlauf", "Spielzeiten sinken nie" if not drops else f"{len(drops)}× gesunken, z. B. {drops[0]}")
    add(not big, "Verlauf", "kein Tag über 20 Std" if not big else "Tage über 20 Std: " + ", ".join(f"{d} ({h} Std)" for d, h in big[:4]))
    # Quellen
    cache = load_cache()
    for name, src in (cache.get("sources") or {}).items():
        add(src.get("ok"), "Quelle " + name, "ok" if src.get("ok") else src.get("error", "Fehler"))
    # Tier-Liste darf nicht schrumpfen, ohne dass du etwas änderst
    n_t = sum(1 for v in load_meta().values() if v.get("tier"))
    last = kv_get("check_tiers", None)
    add(last is None or n_t >= last - 3, "Tier-Liste", f"{n_t} Einordnungen" + (f" (vorher {last})" if last is not None else ""))
    kv_set("check_tiers", n_t)
    # Begleiter
    raw = kv_get("pet", {}) or {}
    sps = [p["sp"] for p in raw.get("pets") or []]
    add(len(sps) == len(set(sps)), "Begleiter", "keine doppelten Arten" if len(sps) == len(set(sps)) else f"doppelt: {sps}")
    try:
        st = pet_state(cfg, force=True)
        eggs_xp = [p for p in st.get("pets") or [] if not p["stage"] and p["xp"]]
        add(not eggs_xp, "Begleiter", "Eier ohne Erfahrung" if not eggs_xp else f"Ei mit Erfahrung: {eggs_xp[0]['species']}")
        bad_lv = [p for p in st.get("pets") or [] if p["stage"] and (p["stage"] == 1) != (p["lvl"] < PET_STAGE_LV[0])]
        add(not bad_lv, "Begleiter", "Stufe passt zum Level" if not bad_lv else f"{bad_lv[0]['species']}: Stufe {bad_lv[0]['stage']} bei Level {bad_lv[0]['lvl']}")
        fast = [b for b in st.get("history") or [] if b["won"] and b["kind"] == "week" and b.get("win_day")
                and (dt.date.fromisoformat(b["win_day"]) - dt.date.fromisoformat(b["from"])).days < 2 and b["from"] > str(raw.get("epoch") or "")]
        add(not fast, "Begleiter", "kein Wochen-Boss in unter 2 Tagen" if not fast else f"{len(fast)} Wochen-Bosse zu schnell")
        add(st.get("active") in {p["id"] for p in st.get("pets") or []}, "Begleiter", "aktiver Begleiter vorhanden")
    except Exception as e:
        add(False, "Begleiter", "Berechnung fehlgeschlagen: " + err_text(e))
    # Cloud
    add(not cloud_ready(cfg) or not cloud_state.get("error"), "Handy-Cloud", cloud_state.get("error") or "ok")
    return out


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
            if k == "pet":   # Begleiter wird zusammengeführt, nicht überschrieben
                kv_set(k, merge_pet(e.get("value")))
                n += 1
            elif k == "plan_pushed":
                kv_set(k, {**(kv_get(k, {}) or {}), **clean_kv(k, e.get("value"))})
                n += 1
            elif k in KV_KEYS and k != "wrapped" and not (upd and (parse_iso(upd) or 0) >= (e.get("ts") or 0) / 1000):
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
        old = load_cache()
        old_games = old.get("games") or []
        if not kv_get("reconcile_since"):
            kv_set("reconcile_since", (dt.date.today() + dt.timedelta(days=1)).isoformat())
        src_ok_prev = kv_get("src_ok", {}) or {}
        if cfg["psn_npsso"].strip():
            try:
                skipped = []
                g = fetch_psn(cfg, skipped)
                psn_games = g
                direct.add("PlayStation")
                sources["PlayStation"] = {"ok": True, "count": len(g), "minutes": sum(x["minutes"] for x in g),
                                          "info": f"{len(skipped)} Apps ausgeblendet: {', '.join(sorted(set(n.split(' [')[0] for n in skipped))[:8])}" if skipped else None}
            except Exception as e:
                # Ausfall: letzten Stand behalten, damit nichts verschwindet (Tier-Liste, Statistik, Begleiter)
                keep = [x for x in old_games if str(x.get("id", "")).startswith("psn_")]
                psn_games = keep
                if keep:
                    direct.add("PlayStation")
                sources["PlayStation"] = {"ok": False, "error": err_text(e), "count": len(keep), "kept": bool(keep)}
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
        try:
            fix_genres(games)
        except Exception as e:
            print("  Genres:", e)
        if not sources.get("Playnite", {}).get("ok", True):   # Playnite-Export kaputt: PC-Spiele vom letzten Mal behalten
            have = {g["id"] for g in games}
            games += [x for x in old_games if str(x.get("id", "")).startswith(("pn_", "pnc_", "steam_")) and x["id"] not in have]
        for name, s in sources.items():
            print(f"  {name}: " + ((f"{s['count']} Spiele" if s.get("count") is not None else s.get("info") or "ok") if s["ok"] else "FEHLER - " + s["error"] + (" (letzter Stand bleibt)" if s.get("kept") else "")))
        try:
            repair_history_once(games)
        except Exception as e:
            print("  Korrektur:", e)
        try:
            n = reconcile_history(games, old.get("last_sync"))
            if n:
                print(f"  Spielzeit nachgetragen: {n}")
        except Exception as e:
            traceback.print_exc()
            print("  Nachtrag:", e)
        write_json(CACHE_PATH, {"last_sync": now_iso(), "sources": sources, "games": games, "pn_images": PN_IMAGES})
        src_now = dict(src_ok_prev, **{k: now_iso() for k, v in sources.items() if v.get("ok") and k in ("PlayStation", "Xbox", "Playnite")})
        kv_set("src_ok", src_now)
        sd = kv_get("src_day", {}) or {}
        sd[dt.date.today().isoformat()] = src_now   # von wann die Daten jeder Quelle im heutigen Stand sind
        kv_set("src_day", dict(sorted(sd.items())[-120:]))
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


# Playnite holt Genres manchmal vom falschen Spiel (Marvel Rivals → „Racing, RPG“). Steam kennt die echten,
# aber nur grob. Wir behalten Playnite-Genres nur, wenn Steam sie bestätigt, und ergänzen Steams eigene.
_STEAM_NEEDS = {"racing": "Racing", "role-playing (rpg)": "RPG", "rpg": "RPG", "strategy": "Strategy",
                "real time strategy (rts)": "Strategy", "turn-based strategy (tbs)": "Strategy", "tactical": "Strategy",
                "sport": "Sports", "sports": "Sports", "simulator": "Simulation", "simulation": "Simulation",
                "adventure": "Adventure", "point-and-click": "Adventure", "visual novel": "Adventure",
                "indie": "Indie", "massively multiplayer": "Massively Multiplayer", "mmo": "Massively Multiplayer"}
_STEAM_SKIP = {"free to play", "free-to-play", "early access", "utilities", "software", "design & illustration",
               "animation & modeling", "audio production", "video production", "web publishing", "education",
               "game development", "photo editing", "accounting", "movie", "documentary", "episodic", "short", "tutorial",
               "violent", "gore", "nudity", "sexual content"}


def fix_genres(games):
    sc = (read_json(SCORES_PATH, {}) or {}).get("scores", {})
    n = 0
    for g in games:
        st = (sc.get(str(g.get("steam_appid") or "")) or {}).get("gn")
        if not st:
            continue
        sset = {x.lower() for x in st}
        keep = [x for x in g.get("genres") or [] if _STEAM_NEEDS.get(x.lower(), "Action").lower() in sset
                or (_STEAM_NEEDS.get(x.lower()) is None and ("action" in sset or "casual" in sset))]
        have = {x.lower() for x in keep}
        for x in st:
            xl = x.lower()
            if xl in _STEAM_SKIP or xl in have:
                continue
            if xl == "action" and keep:     # „Shooter“ o. Ä. ist schon genauer
                continue
            if any(_STEAM_NEEDS.get(k.lower(), "").lower() == xl for k in keep):
                continue
            keep.append(x)
            have.add(xl)
        if keep != g.get("genres"):
            g["genres"] = keep
            n += 1
    return n


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
                extras_state["error"] = None
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


def new_games_seen(games=None):
    """Spiele, die nach Beginn der Aufzeichnung neu aufgetaucht sind (auch Konsole), als {Spielschlüssel: Tag}.
    Importe mit viel alter Spielzeit zählen nicht als neu."""
    con = history_con()
    rows = con.execute("SELECT s.id, s.day, s.minutes FROM snapshots s JOIN (SELECT id, MIN(day) d FROM snapshots GROUP BY id) f "
                       "ON s.id = f.id AND s.day = f.d WHERE f.d > (SELECT MIN(day) FROM snapshots)").fetchall()
    con.close()
    titles = {g["id"]: norm_title(g["title"]) for g in (games if games is not None else load_cache().get("games", []))}
    out = {}
    for gid, day, m in rows:
        k = titles.get(gid)
        if k and m <= IMPORT_MIN and (k not in out or day < out[k]):
            out[k] = day
    return out


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


def _snap_series(bounds, per_key=False):
    """Spielzeit je Zeitraum aus den täglichen Ständen. bounds: [(start, ende)] als date.
    Liefert {index: Minuten} bzw. mit per_key {index: {spiel: Minuten}}. Zeiträume ohne Ausgangsstand fehlen;
    im Zeitraum, in dem die Aufzeichnung begann, wird ab dem ersten Stand gezählt."""
    con = history_con()
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
    out = {}
    if not days:
        con.close()
        return out
    keymap = {}
    if per_key:
        keymap = {g["id"]: norm_title(g["title"]) for g in load_cache().get("games", [])}
    cache = {}
    first = first_seen_minutes(con)
    def snap(day):
        if day not in cache:
            cache[day] = {i: (m, p) for i, p, m in con.execute("SELECT id, platform, minutes FROM snapshots WHERE day = ?", (day,))}
        return cache[day]
    def last_before(d):
        iso = d.isoformat()
        prev = [x for x in days if x < iso]
        return prev[-1] if prev else None
    for n, (a, z) in enumerate(bounds):
        b, e = last_before(a), last_before(z)
        if not b and e and days[0] >= a.isoformat():
            b = days[0]
        if not b or not e or b == e:
            if b and e:
                out[n] = {} if per_key else 0
            continue
        A, B = snap(b), snap(e)
        devs = {p for _, p in A.values()}
        acc = {} if per_key else 0
        for gid, (m, p) in B.items():
            if gid in A:
                d = max(0, m - A[gid][0])
            elif p in devs:
                fm = first.get(gid, 0)
                d = max(0, m - fm) if fm > IMPORT_MIN else m
            else:
                d = 0
            if not d:
                continue
            if per_key:
                k = keymap.get(gid)
                if k:
                    acc[k] = acc.get(k, 0) + d
            else:
                acc += d
        out[n] = acc
    con.close()
    return out


def _week_bounds(n):
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    return [(monday - dt.timedelta(weeks=i), monday - dt.timedelta(weeks=i) + dt.timedelta(days=7)) for i in range(n - 1, -1, -1)]


def _month_bounds(n):
    today = dt.date.today()
    first = today.replace(day=1)
    out = []
    for i in range(n - 1, -1, -1):
        y, m = first.year, first.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        a = dt.date(y, m, 1)
        z = dt.date(y + (m == 12), m % 12 + 1, 1)
        out.append((a, z))
    return out


def weekly_minutes(n=60):
    """Spielzeit pro Kalenderwoche (Mo–So): [[Montag, Minuten], ...], älteste zuerst."""
    b = _week_bounds(n)
    r = _snap_series(b)
    return [[b[i][0].isoformat(), int(v)] for i, v in sorted(r.items())]


def monthly_minutes(n=13):
    """Spielzeit pro Monat: {"YYYY-MM": Minuten}."""
    b = _month_bounds(n)
    r = _snap_series(b)
    return {b[i][0].strftime("%Y-%m"): int(v) for i, v in r.items()}


_ghist = {"t": 0, "v": None}


def game_history():
    """Spielverlauf je Spiel: {"weeks": [Montage], "g": {spiel: [[wochenindex, Minuten], ...]}} (nur Wochen mit Spielzeit)."""
    if time.time() - _ghist["t"] < 900 and _ghist["v"] is not None:
        return _ghist["v"]
    con = history_con()
    first = con.execute("SELECT MIN(day) FROM snapshots").fetchone()[0]
    con.close()
    out = {"weeks": [], "g": {}}
    if first:
        today = dt.date.today()
        n = min(260, (today - dt.date.fromisoformat(first)).days // 7 + 2)
        b = _week_bounds(n)
        r = _snap_series(b, per_key=True)
        idx = sorted(r)
        out["weeks"] = [b[i][0].isoformat() for i in idx]
        for j, i in enumerate(idx):
            for k, m in r[i].items():
                out["g"].setdefault(k, []).append([j, int(m)])
    _ghist.update(t=time.time(), v=out)
    return out


# ================================================================ Begleiter (Eier, Level, Bosse, Beute)

PET_SPECIES = {
    "brasu": {"name": "Brasu", "stages": ["Funkel", "Flimm", "Solvar"], "fav": "Action", "start": True,
              "match": ("action", "shooter", "hack and slash", "fighting", "platform", "arcade")},
    "neruma": {"name": "Neruma", "stages": ["Blupp", "Wellen", "Thalune"], "fav": "Rollenspiele", "start": True,
               "match": ("role-playing", "rpg")},
    "moselle": {"name": "Moselle", "stages": ["Spross", "Rankel", "Sylvara"], "fav": "Cozy & Simulation", "start": True,
                "match": ("simulator", "simulation", "puzzle", "card", "board", "music", "sport")},
    "crysto": {"name": "Crysto", "stages": ["Splitter", "Facett", "Prismarch"], "fav": "Strategie", "start": False,
               "match": ("strategy", "tactical", "rts", "tbs", "moba")},
    "nocti": {"name": "Nocti", "stages": ["Flausch", "Dämmer", "Velunara"], "fav": "Horror & Mystery", "start": False,
              "match": ("horror", "survival", "mystery", "point-and-click", "visual novel")},
    "koppla": {"name": "Koppla", "stages": ["Piko & Pika", "Echo & Edda", "Harmonia"], "fav": "Koop & Mehrspieler", "start": False,
               "match": ("co-op", "coop", "co-operative", "multiplayer", "koop")},
    # Saison-Arten: Eier gibt es nur in ihrer Jahreszeit
    "frostel": {"name": "Frostel", "stages": ["Flocki", "Frostel", "Glaziara"], "fav": "Survival & Erkundung", "start": False,
                "match": ("survival", "exploration"), "season": (12, 1, 2)},
    "bluma": {"name": "Bluma", "stages": ["Blümchen", "Blumhopp", "Florabella"], "fav": "Gemütlich & Kreativ", "start": False,
              "match": ("sandbox", "building", "casual", "family", "farming", "simulator"), "season": (3, 4, 5)},
    "sola": {"name": "Sola", "stages": ["Funkfeder", "Solaris", "Sonnara"], "fav": "Rennen & Sport", "start": False,
             "match": ("racing", "sport", "driving", "fighting"), "season": (6, 7, 8)},
    "herzi": {"name": "Herzi", "stages": ["Herzchen", "Schnurrlieb", "Amorina"], "fav": "Indie & Party", "start": False,
              "match": ("indie", "party", "music", "rhythm", "platform"), "season": (2,)},
    "tavik": {"name": "Tavik", "stages": ["Klick", "Takt", "Chronox"], "fav": "Technik & Aufbau", "start": False,
              "match": ("automation", "building", "crafting", "management", "factory", "city builder")},
    "kairo": {"name": "Kairo", "stages": ["Dune", "Siro", "Zephar"], "fav": "Abenteuer & Open World", "start": False,
              "match": ("adventure", "open world")},
    "vexa": {"name": "Vexa", "stages": ["Klecks", "Schmier", "Inkyra"], "fav": "Puzzle & Knobeln", "start": False,
             "match": ("puzzle", "logic", "point-and-click")},
    "aeru": {"name": "Aeru", "stages": ["Wölkchen", "Böe", "Tempestral"], "fav": "Tempo & Plattformer", "start": False,
             "match": ("platform", "racing", "flight", "arcade")},
    "moru": {"name": "Moru", "stages": ["Schlummer", "Somnia", "Oneira"], "fav": "Story & Atmosphäre", "start": False,
             "match": ("story", "narrative", "visual novel", "walking simulator", "atmospheric")},
    "grumbo": {"name": "Grumbo", "stages": ["Krümel", "Brocken", "Monolith"], "fav": "Survival & Crafting", "start": False,
               "match": ("survival", "crafting", "sandbox")},
    "blossom": {"name": "Blossom", "stages": ["Knospi", "Blütenlauf", "Floralis"], "fav": "Gemütlich & Natur", "start": False,
                "match": ("farming", "life sim", "cozy", "simulation"), "season": (3, 4, 5)},
    "solero": {"name": "Solero", "stages": ["Krabbi", "Korallo", "Solmara"], "fav": "Party & Sport", "start": False,
               "match": ("party", "sport", "racing", "casual"), "season": (6, 7, 8)},
    "autumo": {"name": "Autumo", "stages": ["Stups", "Laubbert", "Aurorix"], "fav": "Abenteuer & Rätsel", "start": False,
               "match": ("adventure", "mystery", "puzzle"), "season": (9, 10, 11)},
    "frostius": {"name": "Frostius", "stages": ["Flocke", "Kristall", "Borealis"], "fav": "Strategie & Taktik", "start": False,
                 "match": ("strategy", "tactical"), "season": (12, 1, 2)},
}
PET_STARS = (30, 35, 40)          # nach der Endform: Sterne als Ziel
PET_STAGE_LV = (10, 25)          # Stufe 2 ab Level 10, Stufe 3 ab Level 25
PET_HATCH_DAYS = 3               # Spieltage (mind. 15 Min) bis zum Schlüpfen
PET_DAY_CAP = 180                # höchstens 3 Std Spielzeit pro Tag zählen
PET_ACC = {"schleife": "Schleife", "headset": "Gaming-Headset", "brille": "Sonnenbrille", "krone": "Krone", "zauberhut": "Zauberhut",
           "blumen": "Blumenkranz", "herz": "Herz-Anhänger", "muetze": "Bommelmütze", "pirat": "Piratenhut", "weihnacht": "Weihnachtsmütze",
           "kuerbis": "Kürbis-Hütchen", "wikinger": "Wikingerhelm", "party": "Partyhütchen", "halo": "Heiligenschein",
           "diadem": "Kristall-Diadem", "kapitaen": "Kapitänsmütze",
           "herzreif": "Herz-Haarreif", "hasenohren": "Hasenohren", "sonnenhut": "Sonnenhut", "hexenhut": "Hexenhut",
           "schal": "Ringelschal", "geweih": "Rentier-Geweih", "ohrenschuetzer": "Ohrenschützer", "wichtel": "Wichtelmütze",
           "lichterkette": "Lichterkette", "sternkrone": "Schneestern-Krone", "zuckerstange": "Zuckerstange", "silvester": "Silvester-Brille"}
PET_HAB = {"lichtung": "Lichtung", "sterne": "Sternenhimmel", "strand": "Strand", "zimmer": "Gaming-Zimmer", "vulkan": "Vulkan",
           "winter": "Winterwald", "kristall": "Kristallhöhle", "riff": "Unterwasser-Riff",
           "weihnachtsstube": "Weihnachtsstube", "silvester": "Silvester-Dach", "wunschbaum": "Wunschbaum"}
# Adventskalender: Belohnung pro Türchen (Rest: Geschichte, Fragen, Erinnerungen – die baut die App)
ADVENT_REWARDS = {1: ("acc", "schal"), 4: ("acc", "geweih"), 7: ("acc", "ohrenschuetzer"), 11: ("acc", "wichtel"),
                  15: ("acc", "lichterkette"), 18: ("acc", "zuckerstange"), 21: ("acc", "sternkrone"), 24: ("hab", "weihnachtsstube")}
# Beute nach Anzahl Siege (Wochen-, Saison- und Monats-Bosse zusammen)
PET_LOOT = {1: ("acc", "schleife"), 3: ("hab", "sterne"), 5: ("acc", "headset"), 7: ("theme", "drachenglut"),
            9: ("hab", "strand"), 11: ("acc", "brille"), 13: ("acc", "pirat"), 15: ("hab", "zimmer"), 17: ("acc", "krone"),
            19: ("acc", "muetze"), 21: ("theme", "mondnacht"), 23: ("hab", "kristall"), 25: ("acc", "zauberhut"),
            28: ("acc", "wikinger"), 31: ("hab", "vulkan"), 34: ("shiny", ""), 37: ("acc", "halo"), 40: ("hab", "riff"),
            44: ("acc", "blumen"), 48: ("acc", "kapitaen"), 52: ("acc", "diadem"), 60: ("shiny", ""), 75: ("shiny", "")}
# Eier gibt es zufällig nach Boss-Siegen. Seltenheit: 1 = gewöhnlich, 2 = selten, 3 = sehr selten
# 1 gewöhnlich · 2 selten · 3 sehr selten. Saison-Tiere: pro Jahreszeit ein seltenes und ein sehr seltenes
PET_RARITY = {"brasu": 1, "neruma": 1, "moselle": 1, "kairo": 1, "grumbo": 1,
              "crysto": 2, "nocti": 2, "tavik": 2, "aeru": 2, "vexa": 2,
              "moru": 3, "koppla": 3,
              "bluma": 2, "blossom": 3, "sola": 2, "solero": 3, "autumo": 2, "frostel": 2, "frostius": 3, "herzi": 3}
PET_SEASONS = {"Frühling": (3, 4, 5), "Sommer": (6, 7, 8), "Herbst": (9, 10, 11), "Winter": (12, 1, 2)}
PET_RARITY_W = {1: 10, 2: 4, 3: 1.2}
PET_EGG_CHANCE = {"week": 0.12, "month": 0.35, "raid": 0.35}
DUO_ATTACK, DUO_ATTACK_FROM = 45, 15   # Bindung: Duo-Angriff ab 15 gemeinsamen Abenden (Stufe „Freunde“)
PET_EGG_PITY = 8                 # spätestens nach 8 Siegen ohne Ei kommt eins
PET_WEEKLY = [("golem", "Backlog-Golem", "🗿", "Durchgespielte Spiele treffen ihn dreifach."),
              ("hydra", "Genre-Hydra", "🐍", "Jedes verschiedene Genre der Woche schlägt einen Kopf ab."),
              ("drache", "Trophäen-Drache", "🐉", "Erfolge und Trophäen sind kritische Treffer."),
              ("nachtmahr", "Nachtmahr", "👻", "Sessions, die vor Mitternacht enden, treffen doppelt – Zocken nach Mitternacht heilt ihn."),
              ("sphinx", "Entdecker-Sphinx", "🦁", "Jedes neu angefangene Spiel trifft hart."),
              ("gremlin", "Lag-Gremlin", "👾", "Jeder Tag, an dem du spielst, bringt ihn ins Stottern."),
              ("zeitfresser", "Zeitfresser", "⏳", "Ausgeruhte Spieltage nach einer Pause treffen ihn besonders."),
              ("phantom", "Neon-Phantom", "🌀", "Das Lieblingsgenre deines Begleiters trifft doppelt."),
              ("wolke", "Couch-Wolke", "☁️", "Kurze Sessions an vielen Tagen pusten sie weg."),
              ("koloss", "Pixel-Koloss", "🧱", "Keine Schwäche – hier zählt einfach jede Stunde.")]
PET_SEASON = {2: ("amor", "Amors Schatten", "💘", "Gemeinsame Abende treffen dreifach – Valentins-Special!"),
              4: ("osterhase", "Chaos-Hase", "🐰", "Neu angefangene Spiele sind versteckte Eier – Oster-Special!"),
              7: ("sonnenwurm", "Sonnenwurm", "☀️", "Ausgeruhte Spieltage kühlen ihn ab – Sommer-Special!"),
              10: ("kuerbis", "Kürbiskönig", "🎃", "Grusel, Horror und Mystery treffen dreifach – Halloween-Special!"),
              12: ("yeti", "Frost-Yeti", "❄️", "Gemütliche Spiele und Ruhetage tauen ihn auf – Winter-Special!")}
PET_MONTHLY = [("titan", "Uralter Titan", "🌋", "Durchgespielte Spiele sind seine Schwäche."),
               ("leviathan", "Leviathan", "🐋", "Abwechslung bei den Genres schwächt ihn."),
               ("phoenix", "Schattenphönix", "🦅", "Neu angefangene Spiele löschen sein Feuer."),
               ("mecha", "Mecha-Wyrm", "🤖", "Erfolge und Trophäen bringen ihn zum Kurzschluss.")]
# Reihenfolge so, dass der laufende Monat (Nov 2026: Zwillingsschatten) gleich bleibt
PET_RAID = [("kraken", "Koop-Kraken", "🐙", "Nur gemeinsame Abende und Spiele, die ihr beide durchspielt, treffen ihn."),
            ("sturmriese", "Sturmriese", "🌩️", "Nur gemeinsame Abende und Spiele, die ihr beide durchspielt, treffen ihn."),
            ("drachenpaar", "Drachenpaar", "🐲", "Nur gemeinsame Abende und Spiele, die ihr beide durchspielt, treffen sie."),
            ("zwilling", "Zwillingsschatten", "👥", "Nur gemeinsame Abende und Spiele, die ihr beide durchspielt, treffen sie.")]


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    mo, da = divmod(h + l_ - 7 * m + 114, 31)
    return dt.date(y, mo, da + 1)


def pet_special_boss(week_start):
    """Sonder-Boss nur in der Woche des Anlasses (nicht den ganzen Monat lang)."""
    y = week_start.year
    days = {2: dt.date(y, 2, 14), 4: _easter(y), 7: dt.date(y, 7, 15), 10: dt.date(y, 10, 31), 12: dt.date(y, 12, 24)}
    for m, d in days.items():
        for dd in (d, d.replace(year=y + 1) if m != 4 else _easter(y + 1)):
            if week_start <= dd <= week_start + dt.timedelta(days=6):
                return PET_SEASON[m]
    return None
PET_QUESTS = [("days4", "Spiel an 4 verschiedenen Tagen", 4), ("new1", "Fang ein neues Spiel an", 1), ("rest1", "Gönn dir einen Ruhetag", 1),
              ("fav120", "Spiel 2 Std im Lieblingsgenre", 120), ("ach10", "Hol dir 10 Erfolge", 10), ("done1", "Spiel ein Spiel durch", 1),
              ("early3", "Beende 3 Abende vor Mitternacht", 3), ("genre3", "Spiel 3 verschiedene Genres", 3), ("eve1", "Ein gemeinsamer Abend", 1),
              ("con60", "Spiel 1 Std auf der Konsole", 60), ("pc60", "Spiel 1 Std am PC", 60), ("back1", "Hol ein Spiel zurück, das 30 Tage lag", 1),
              ("long90", "Eine lange Session: 90 Min am Stück in einem Spiel", 1), ("days5", "Spiel an 5 verschiedenen Tagen", 5),
              ("var3", "Spiel 3 verschiedene Spiele", 3), ("same300", "Spiel ein Spiel insgesamt 5 Std", 300), ("ach25", "Hol dir 25 Erfolge", 25),
              ("wkend2", "Spiel am Samstag und am Sonntag", 2), ("fav300", "Spiel 5 Std im Lieblingsgenre", 300), ("rest2", "Gönn dir 2 Ruhetage", 2),
              ("nolate", "Eine ganze Woche nie nach Mitternacht zocken", 1)]
PET_DUO_QUESTS = [("eve2", "Zwei gemeinsame Abende", 2), ("both8", "Zusammen 8 Std spielen", 480),
                  ("relay3", "3× den Staffelstab übergeben", 3)]
PET_COZY = ("simulator", "simulation", "puzzle", "card", "board", "music")
PET_SPOOKY = ("horror", "survival", "mystery")


def pet_xp_for(level):
    return int(round(40 * (level - 1) ** 1.9)) if level > 1 else 0


def pet_level(xp):
    lv = 1
    while lv < 60 and xp >= pet_xp_for(lv + 1):
        lv += 1
    return lv


def _pet_game_info():
    info = {}
    for g in load_cache().get("games", []):
        txt = " ".join([str(x).lower() for x in (g.get("genres") or []) + (g.get("features") or []) + (g.get("tags") or [])])
        info[g["id"]] = {"k": norm_title(g["title"]), "genres": [str(x) for x in g.get("genres") or []], "txt": txt, "dev": g.get("device")}
    return info


def _pet_days(since):
    """Spielzeit pro Tag ab since (date): {tag: {"min", "per": {id: Minuten}, "started": [ids]}}."""
    con = history_con()
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
    out, prev = {}, None
    s_iso = since.isoformat()
    rsince = kv_get("reconcile_since") or "9999"   # ab hier trägt der Abgleich neue Spiele ausdrücklich ein
    for d in days:
        snap = {i: (m, p) for i, p, m in con.execute("SELECT id, platform, minutes FROM snapshots WHERE day = ?", (d,))}
        if prev is not None and d >= s_iso:
            devs = {p for _, p in prev.values()}
            per, started = {}, []
            for gid, (m, p) in snap.items():
                if gid in prev:
                    dm = m - prev[gid][0]
                    if dm > 0 and prev[gid][0] == 0:
                        started.append(gid)
                elif p in devs and 0 < m <= 600 and not d < rsince:
                    dm = 0          # ohne ausdrücklichen 0-Eintrag (macht der Nachtrag) ist ein neues Spiel Ausgangsstand
                elif p in devs and 0 < m <= 600:
                    dm = m          # alte Daten (vor dem Nachtrag): neu und an einem Tag plausibel gespielt
                    started.append(gid)
                else:
                    dm = 0          # Import mit alter Spielzeit (z. B. neuer Launcher) – zählt nicht als heute gespielt
                if dm > 0:
                    per[gid] = dm
            if len(started) > 8:    # Massen-Import
                started = []
            out[d] = {"min": sum(per.values()), "per": per, "started": started}
        prev = snap
    con.close()
    # AFK-Zeit (Spiel offen, keine Eingabe) abziehen – zählt für Begleiter und Bosse nicht
    try:
        afk = TRACKER.afk_minutes(s_iso)
    except Exception:
        afk = {}
    if afk:
        keys = {g["id"]: norm_title(g["title"]) for g in load_cache().get("games", []) if g.get("device") == "PC"}
        for d, per_k in afk.items():
            D = out.get(d)
            if not D:
                continue
            for gid, m in list(D["per"].items()):
                cut = min(m, per_k.get(keys.get(gid), 0))
                if cut:
                    D["per"][gid] = m - cut
                    D["afk"] = D.get("afk", 0) + cut
            D["per"] = {k: v for k, v in D["per"].items() if v > 0}
            D["min"] = sum(D["per"].values())
    return out


_pet_cache = {"t": 0, "v": None}


def pet_act(raw, pets):
    """Wechsel-Liste [[Zeitpunkt, Begleiter, Minuten heute beim Wechsel]], zeitlich sortiert (stabil).
    Ältere Einträge haben nur ein Datum; vor dem Neustart des Begleiter-Spiels zählt nur der letzte Stand."""
    ids = {p["id"] for p in pets}
    act = []
    for n, a in enumerate(raw.get("act") or []):
        if isinstance(a, list) and len(a) >= 2 and a[1] in ids:
            act.append((str(a[0]), n, a[1], a[2] if len(a) > 2 and isinstance(a[2], (int, float)) else None))
    act.sort(key=lambda x: (x[0], x[1]))
    act = [[ts, pid, mk] for ts, _, pid, mk in act]
    ep = raw.get("epoch")
    if ep:
        before = [a for a in act if a[0][:10] < ep]
        act = ([[ep, before[-1][1], None]] if before else []) + [a for a in act if a[0][:10] >= ep]
    if not act:
        act = [[pets[0]["start"], pets[0]["id"], None]]
    if act[0][0][:10] > min(p["start"] for p in pets):
        act.insert(0, [min(p["start"] for p in pets), act[0][1], None])
    return act


def pet_state(cfg, force=False):
    """Berechnet den ganzen Begleiter-Stand aus dem echten Verlauf (deterministisch, nichts doppelt)."""
    if not force and _pet_cache["v"] is not None and time.time() - _pet_cache["t"] < 300:
        return _pet_cache["v"]
    raw = kv_get("pet", {}) or {}
    pets = raw.get("pets") or []
    if not pets:
        res = {"pets": [], "starter": [k for k, v in PET_SPECIES.items() if v["start"]]}
        _pet_cache.update(t=time.time(), v=res)
        return res
    today = dt.date.today()
    T = today.isoformat()
    epoch = dt.date.fromisoformat(raw["epoch"]) if raw.get("epoch") else None
    start = min(dt.date.fromisoformat(p["start"]) for p in pets)
    if epoch:
        start = max(start, epoch)
    act = pet_act(raw, pets)
    def active_on(d):
        """Wer am Ende des Tages d (oder zu einem Zeitpunkt d) an deiner Seite ist."""
        cur = act[0][1]
        for ts, pid, _ in act:
            if ts[:len(d)] <= d if len(d) > 10 else ts[:10] <= d:
                cur = pid
        return cur
    def segments(iso, total):
        """[(Begleiter, Minuten)] für einen Tag: Wechsel am Tag teilen die Spielzeit an der gemerkten Stelle."""
        cur = act[0][1]
        todays = []
        for ts, pid, mark in act:
            if ts[:10] < iso:
                cur = pid
            elif ts[:10] == iso:
                todays.append((ts, pid, mark))
        out, pos = [], 0
        for ts, pid, mark in todays:
            if mark is None:   # alter Eintrag (nur Datum): ganzer Tag; nachträglich übernommen: Tagesende
                mk = pos if len(ts) == 10 else total
            else:
                mk = max(pos, min(total, int(mark)))
            if mk > pos:
                out.append((cur, mk - pos))
                pos = mk
            cur = pid
        out.append((cur, total - pos))
        return out
    gi = _pet_game_info()
    days = _pet_days(start)
    if epoch:
        pets = [dict(p, start=max(p["start"], epoch.isoformat())) for p in pets]
    titles = {g["k"]: g["k"] for g in gi.values()}
    titles.update({norm_title(g["title"]): g["title"] for g in load_cache().get("games", [])})
    # Ereignisse
    ev = {}
    def add(day, key, n=1):
        ev.setdefault(day, {}).setdefault(key, 0)
        ev[day][key] += n
    seen, completions = set(), []
    slog = load_status_log(800)
    for k, st, at in slog:
        day = str(at)[:10]
        if st in ("durchgespielt", "komplett") and day >= start.isoformat() and (k, st) not in seen:
            seen.add((k, st))
            add(day, "done" if st == "durchgespielt" else "full")
            completions.append((str(at), k, st))
    try:
        sx = read_json(STEAMX_PATH, {}) or {}
        for a in (sx.get("ach") or {}).values():
            for u in a.get("u") or []:
                ts = int(u[2] or 0)
                if ts:
                    day = time.strftime("%Y-%m-%d", time.localtime(ts))
                    if day >= start.isoformat():
                        add(day, "ach")
    except Exception:
        pass
    sess = tracker.load_sessions(APP_DB, int(dt.datetime.combine(start, dt.time()).timestamp()))
    for k, s0, sec in sess:   # Nachtmahr: vor bzw. nach Mitternacht
        late = sum(5 for t in range(s0, s0 + sec, 300) if dt.datetime.fromtimestamp(t).hour < 5)
        day = (dt.datetime.fromtimestamp(s0) - dt.timedelta(hours=5)).date().isoformat()
        add(day, "late", late)
        add(day, "early", max(0, sec // 60 - late))
    pa = kv_get("partner", None) if cfg.get("partner_url") and cfg.get("partner_code") else None
    evenings = set()
    if pa and pa.get("sess"):
        by = {}
        for k, s2, sec2 in pa["sess"]:
            by.setdefault(k, []).append((s2, s2 + sec2))
        for k, s0, sec in sess:
            for a2, b2 in by.get(k, []):
                if min(s0 + sec, b2) - max(s0, a2) >= 900:
                    evenings.add((dt.datetime.fromtimestamp(max(s0, a2)) - dt.timedelta(hours=5)).date().isoformat())
    for d in evenings:
        add(d, "evening")
    # 1) Erfahrung pro Tag – bei Wechseln am selben Tag wird die Spielzeit an der Wechselstelle geteilt
    sp_of = {p["id"]: p["sp"] for p in pets}
    pstart = {p["id"]: p["start"] for p in pets}
    daylog, d, prev_min = {}, start, None
    while d <= today:
        iso = d.isoformat()
        D = days.get(iso, {"min": 0, "per": {}, "started": []})
        E = ev.get(iso, {})
        segs = segments(iso, D["min"])
        pid = segs[-1][0]          # wer am Ende des Tages an deiner Seite ist
        raw_m = min(PET_DAY_CAP, D["min"])
        base = min(60, raw_m) + 0.3 * max(0, raw_m - 60)   # erste Stunde voll, danach deutlich weniger
        txt = lambda gid: (gi.get(gid) or {}).get("txt", "")
        rested = base > 0 and prev_min == 0
        started = min(3, len(D["started"]))
        genres = set()
        for gid in D["per"]:
            genres.update((gi.get(gid) or {}).get("genres") or [])
        cozy = sum(m for gid, m in D["per"].items() if any(w in txt(gid) for w in PET_COZY))
        spooky = sum(m for gid, m in D["per"].items() if any(w in txt(gid) for w in PET_SPOOKY))
        split, fav_all = {}, 0
        for spid, mins in segs:
            f = (mins / D["min"]) if D["min"] else (1.0 if spid == pid else 0)
            sp = PET_SPECIES.get(sp_of.get(spid), {})
            fav = f * sum(m for gid, m in D["per"].items() if any(w in txt(gid) for w in sp.get("match", ())))
            fav_all += fav
            pp = {"play": int(base * f), "rested": int(min(30, 0.5 * base) * f) if rested else 0, "fav": int(0.25 * min(fav, base * f))}
            if spid == pid:
                pp.update({"new": 100 * started, "done": 500 * E.get("done", 0) + 800 * E.get("full", 0),
                           "ach": min(250, 10 * E.get("ach", 0)), "evening": 200 * E.get("evening", 0)})
            x = split.setdefault(spid, {"xp": 0, "min": 0, "parts": {}})
            x["xp"] += sum(pp.values())
            x["min"] += mins
            for k_, v_ in pp.items():
                x["parts"][k_] = x["parts"].get(k_, 0) + v_
        parts = split.get(pid, {}).get("parts", {})
        daylog[iso] = {"xp": sum(x["xp"] for x in split.values()), "parts": parts, "min": D["min"], "rested": rested, "pid": pid,
                       "genres": genres, "started": started, "fav": int(fav_all), "cozy": cozy, "spooky": spooky,
                       "played": D["min"] >= 15, "split": split, "per": D["per"], "wd": d.weekday(), **E}
        prev_min = D["min"]
        d += dt.timedelta(days=1)
    last_play = {}
    for iso in sorted(daylog):
        back = [g_ for g_ in daylog[iso]["per"] if g_ in last_play and (dt.date.fromisoformat(iso) - dt.date.fromisoformat(last_play[g_])).days >= 30]
        if back:
            daylog[iso]["back"] = back
        for g_ in daylog[iso]["per"]:
            last_play[g_] = iso
    # 2) Bosse, Aufgaben, Beute
    rewards = []      # (Tag, Grund, Erfahrung)
    loot, history, memories = [], [], []
    got = {"acc": [], "hab": ["lichtung"], "theme": [], "egg": [], "shiny": []}
    rng_seed = (pets[0]["id"] + pets[0]["start"] + str(raw.get("epoch") or ""))
    def give(kind, val, why, day):
        if kind == "shiny":
            val = active_on(day)
        if val in got[kind]:
            return
        got[kind].append(val)
        loot.append({"kind": kind, "val": val, "why": why, "d": day})
    egg_luck = {"since": 0}
    def roll_egg(kind, bid, day):
        """Zufälliges Ei nach einem Boss-Sieg (fest ausgewürfelt, ändert sich beim Neuberechnen nicht)."""
        owned = set(sp_of.values()) | set(got["egg"])
        mon = int(day[5:7])
        pool = [sp for sp in PET_SPECIES if sp not in owned and (sp != "koppla" or pa)
                and (not PET_SPECIES[sp].get("season") or mon in PET_SPECIES[sp]["season"])]
        egg_luck["since"] += 1
        if not pool:
            return
        h = int(hashlib.sha256(f"{rng_seed}|{kind}|{bid}|{day}".encode()).hexdigest(), 16)
        if (h % 10000) / 10000 >= PET_EGG_CHANCE[kind] and egg_luck["since"] < PET_EGG_PITY:
            return
        w = [PET_RARITY_W[PET_RARITY.get(sp, 1)] * (2.5 if PET_SPECIES[sp].get("season") else 1) for sp in pool]   # in der Saison bevorzugt
        r = (h // 10000 % 100000) / 100000 * sum(w)
        for sp, wt in zip(pool, w):
            r -= wt
            if r <= 0:
                break
        egg_luck["since"] = 0
        give("egg", sp, {"week": "Wochen-Boss", "month": "Monats-Boss", "raid": "Raid"}[kind], day)
    def day_dmg(x):
        return min(240, daylog[x]["min"])
    def dmg_of(kind, dl, hp):
        """Schaden = echte Spielzeit (max. 4 Std/Tag). Die Schwäche des Bosses gibt bis zu 35 % extra."""
        tot = sum(day_dmg(x) for x in dl)
        g = set().union(*[daylog[x]["genres"] for x in dl]) if dl else set()
        S_ = lambda key: sum(daylog[x].get(key, 0) for x in dl)
        played = sum(1 for x in dl if daylog[x]["played"])
        rested = sum(1 for x in dl if daylog[x]["rested"])
        extra = {"golem": 150 * (S_("done") + S_("full")), "titan": 300 * (S_("done") + S_("full")),
                 "hydra": 25 * len(g), "leviathan": 40 * len(g),
                 "drache": 4 * S_("ach"), "mecha": 5 * S_("ach"),
                 "nachtmahr": (S_("early") - 2 * S_("late")) // 6, "sphinx": 60 * S_("started"), "phoenix": 90 * S_("started"),
                 "gremlin": 25 * played, "zeitfresser": 80 * rested,
                 "phantom": S_("fav") // 3, "wolke": 30 * played, "amor": 150 * S_("evening"),
                 "osterhase": 80 * S_("started"), "sonnenwurm": 80 * rested, "kuerbis": S_("spooky") // 2,
                 "yeti": S_("cozy") // 2 + 60 * rested}.get(kind, 0)
        duo = DUO_ATTACK * S_("evening") if duo_total >= DUO_ATTACK_FROM else 0      # Duo-Angriff: ab „Freunde“ machen gemeinsame Abende extra Schaden
        return max(0, int(tot + duo + max(-0.2 * hp, min(extra, 0.35 * hp))))
    duo_total = sum(v.get("evening", 0) for v in daylog.values())
    events = []
    monday = start - dt.timedelta(days=start.weekday())
    w = monday
    while w <= today:
        dl = [x for x in ((w + dt.timedelta(days=i)).isoformat() for i in range(7)) if x in daylog]
        events.append((min(w + dt.timedelta(days=6), today), "week", w, dl))
        w += dt.timedelta(days=7)
    m = start.replace(day=1)
    while m <= today:
        nm = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        dl = [x for x in daylog if m.isoformat() <= x < nm.isoformat()]
        end = min(nm - dt.timedelta(days=1), today)
        events.append((end, "month", m, dl))
        if pa:
            events.append((end, "raid", m, dl))
        m = nm
    events.sort(key=lambda e: (e[0], {"week": 0, "month": 1, "raid": 2}[e[1]]))
    current, wins, month_wins, raid_wins, season_won = {}, 0, 0, 0, set()
    pa_done = {(e[1], str(e[3])[:10]) for e in (pa or {}).get("news") or [] if e[0] in ("done", "full")}
    hist_days = _pet_days(start - dt.timedelta(days=35)) if start > dt.date(2000, 1, 1) else {}
    def week_avg(before):
        """Durchschnittliche Spielzeit pro Woche in den 4 Wochen davor – die Bosse passen sich deinem Spielen an."""
        b0 = (before - dt.timedelta(days=28)).isoformat()
        xs = [min(240, v["min"]) for k_, v in hist_days.items() if b0 <= k_ < before.isoformat()]
        if len(xs) < 7:
            return 900
        return sum(xs) / len(xs) * 7
    def too_short(kind, key):
        """Angebrochene Woche/Monat beim Start: Boss erst, wenn genug Zeit bleibt (sonst fällt er sofort)."""
        if kind == "week":
            return key < start and (key + dt.timedelta(days=6) - start).days < 3
        nm = (key.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return key < start and (nm - start).days < 10
    events = [e for e in events if not too_short(e[1], e[2])]
    boss_mem = kv_get("boss_mem", {}) or {}
    boss_mem_new = {}
    for end, kind, key, dl in events:
        live = not end < today
        bkey = f"{raw.get('epoch') or ''}|{kind}|{key.isoformat()}"
        if kind == "week":
            b = pet_special_boss(key) or PET_WEEKLY[(key.toordinal() // 7 + 8) % len(PET_WEEKLY)]   # fortlaufend, auch über den Jahreswechsel
            hp = min(1800, max(200, 0.85 * week_avg(key))) * (1 + 0.02 * min(wins, 20))
            ends = key + dt.timedelta(days=6)
        elif kind == "month":
            b = PET_MONTHLY[(key.year * 12 + key.month + 2) % len(PET_MONTHLY)]   # reihum, keiner zweimal in vier Monaten
            hp = min(6500, max(800, 3.4 * week_avg(key))) * (1 + 0.02 * min(month_wins, 12))
            ends = (key.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        else:
            b = PET_RAID[(key.year * 12 + key.month) % len(PET_RAID)]
            hp = int(1600 * 1.12 ** min(raid_wins, 10))
            ends = (key.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        hp = int(hp)
        if kind == "week" and key < start:   # angefangene Woche vor dem Start: nur anteilig
            hp = int(hp * max(3, (key + dt.timedelta(days=6) - start).days + 1) / 7)
        if kind == "month" and key < start.replace(day=1) or (kind != "week" and dl and dl[0] > key.isoformat()):
            full = (ends - key).days + 1
            hp = int(hp * max(7, len(dl)) / full)
        if bkey in boss_mem:   # einmal festgelegt, bleibt der Boss gleich stark (Nachträge ändern ihn nicht mehr)
            hp = boss_mem[bkey][0]
        def dmg_upto(sub):
            if kind == "raid":
                v = 400 * sum(daylog[x].get("evening", 0) for x in sub)
                mine = {k for at, k, st in completions if sub and sub[0] <= at[:10] <= sub[-1]}
                return v + 800 * len({k for k, at in pa_done if sub and sub[0] <= at <= sub[-1]} & mine)
            return dmg_of(b[0], sub, hp)
        dmg = dmg_upto(dl)
        won = dmg >= hp
        win_day = end.isoformat()
        prev_win = (boss_mem.get(bkey) or [0, None])[1]
        if prev_win and not won:   # einmal besiegt bleibt besiegt
            won, dmg = True, max(dmg, hp)
        if won and prev_win:
            win_day = prev_win
        elif won:   # Siegtag = erster Tag, an dem der Schaden reichte
            for i in range(1, len(dl) + 1):
                if dmg_upto(dl[:i]) >= hp:
                    win_day = dl[i - 1]
                    break
            if win_day > T:
                win_day = T
        boss_mem_new[bkey] = [hp, win_day if won else None]
        rec = {"kind": kind, "id": b[0], "name": b[1], "icon": b[2], "weak": b[3], "hp": hp, "dmg": dmg, "won": won,
               "from": dl[0] if dl else key.isoformat(), "to": end.isoformat(), "live": live, "ends": ends.isoformat(),
               "season": b[0] in ("kuerbis", "yeti", "amor", "osterhase", "sonnenwurm"), "win_day": win_day if won else None}
        if live:
            current[kind] = rec
        if won:
            memories.append({"d": win_day, "i": "⚔️", "t": f"{b[1]} besiegt"})
            if kind == "raid":
                raid_wins += 1
                if raid_wins == 1:
                    give("acc", "herz", "Raid-Sieg", win_day)
                rewards.append((win_day, "raid", 400))
            else:
                wins += 1
                if kind == "month":
                    month_wins += 1
                    rewards.append((win_day, "boss", 300))
                else:
                    rewards.append((win_day, "boss", 100 + 5 * min(wins, 20)))
                if b[0] == "kuerbis":
                    give("acc", "kuerbis", "Halloween-Special", win_day)
                if b[0] == "amor":
                    give("acc", "herzreif", "Valentins-Special", win_day)
                if b[0] == "osterhase":
                    give("acc", "hasenohren", "Oster-Special", win_day)
                if b[0] == "sonnenwurm":
                    give("acc", "sonnenhut", "Sommer-Special", win_day)
                if b[0] == "yeti":
                    give("acc", "weihnacht", "Winter-Special", win_day)
                    give("hab", "winter", "Winter-Special", win_day)
                if wins in PET_LOOT:
                    give(PET_LOOT[wins][0], PET_LOOT[wins][1], f"{wins}. Sieg", win_day)
            roll_egg(kind, b[0], win_day)
            history.append(rec)
        elif not live:
            history.append(rec)
    if completions:
        give("acc", "party", "Erstes durchgespieltes Spiel", completions[0][0][:10])
    have_sp = set(sp_of.values()) | set(got["egg"])
    if all(any(set(PET_SPECIES[k].get("season") or ()) & set(months) for k in have_sp) for months in PET_SEASONS.values()):
        give("theme", "jahreszeiten", "Ein Tier aus jeder Jahreszeit", T)
    if boss_mem_new != {k: v for k, v in boss_mem.items() if k in boss_mem_new} or len(boss_mem_new) > len(boss_mem):
        boss_mem.update(boss_mem_new)
        kv_set("boss_mem", dict(sorted(boss_mem.items())[-200:]))
    # Adventskalender, Silvester, Wunschbaum
    for k in sorted(((kv_get("advent", {}) or {}).get("o") or {})):
        r = ADVENT_REWARDS.get(int(k[8:10]))
        if r and k <= T:
            give(r[0], r[1], f"Adventskalender, {int(k[8:10])}. Türchen", k)
    for y in range(start.year, today.year + 1):
        if f"{y}-12-31" <= T and f"{y}-12-31" >= start.isoformat():
            give("acc", "silvester", f"Silvester {y}", f"{y}-12-31")
            give("hab", "silvester", f"Silvester {y}", f"{y}-12-31")
        if f"{y}-10-31" <= T and f"{y}-10-31" >= start.isoformat():
            give("acc", "hexenhut", f"Halloween {y}", f"{y}-10-31")
        bd = str((kv_get("dates", {}) or {}).get("me") or "")
        if re.fullmatch(r"\d\d-\d\d", bd) and f"{y}-{bd}" <= T and f"{y}-{bd}" >= start.isoformat():
            give("acc", "party", f"Geburtstag {y}", f"{y}-{bd}")
    wd = [w for w in (kv_get("wishes", []) or []) if w.get("done")]
    if wd:
        give("hab", "wunschbaum", "Erster erfüllter Wunsch", dt.date.fromtimestamp(min(w["done"] for w in wd)).isoformat())
    # Geschenke vom Partner
    for gft in ((pa or {}).get("pet") or {}).get("gifts") or []:
        if gft.get("acc") in PET_ACC and str(gft.get("at") or T)[:10] >= start.isoformat():
            give("acc", gft["acc"], f"Geschenk von {pa.get('name') or 'Partner'}", str(gft.get("at") or T)[:10])
    # Wochen-Aufgaben
    quests = {}
    w = monday
    while w <= today:
        dl = [x for x in ((w + dt.timedelta(days=i)).isoformat() for i in range(7)) if x in daylog]
        seed = int(hashlib.sha256(w.isoformat().encode()).hexdigest(), 16)
        pool = [q for q in PET_QUESTS if q[0] != "eve1" or pa]
        pick = []
        while len(pick) < 3:
            q = pool[seed % len(pool)]
            seed //= 7
            if q not in pick:
                pick.append(q)
        qs = []
        for qid, label, target in pick:
            prog, doneday = 0, None
            gen, games_, per_game = set(), set(), {}
            week_full = len(dl) == 7 and dl[-1] < T
            for x in dl:
                L = daylog[x]
                dev_min = lambda pc: sum(m_ for g_, m_ in L["per"].items() if ((gi.get(g_) or {}).get("dev") == "PC") == pc)
                for g_, m_ in L["per"].items():
                    per_game[g_] = per_game.get(g_, 0) + m_
                games_ |= set(L["per"])
                extra_q = {"con60": dev_min(False), "pc60": dev_min(True), "back1": len(L.get("back") or []),
                           "long90": int(any(m_ >= 90 for m_ in L["per"].values())), "days5": int(L["played"]),
                           "ach25": L.get("ach", 0), "wkend2": int(L["played"] and L["wd"] >= 5), "fav300": L["fav"],
                           "rest2": int(L["min"] == 0 and x < T)}
                inc = {"days4": int(L["played"]), "new1": L["started"], "rest1": int(L["min"] == 0 and x < T),
                       "fav120": L["fav"], "ach10": L.get("ach", 0), "done1": L.get("done", 0) + L.get("full", 0),
                       "early3": int(L.get("early", 0) > 0 and not L.get("late", 0)), "eve1": L.get("evening", 0)}.get(qid)
                if qid == "genre3":
                    gen |= L["genres"]
                    prog = len(gen)
                elif qid == "var3":
                    prog = len(games_)
                elif qid == "same300":
                    prog = max(per_game.values() or [0])
                elif qid == "nolate":
                    prog = int(week_full and x == dl[-1] and not any(daylog[y].get("late", 0) for y in dl))
                elif qid in extra_q:
                    prog += extra_q[qid] or 0
                else:
                    prog += inc or 0
                if prog >= target and not doneday:
                    doneday = x
            if doneday:
                rewards.append((doneday, "quest", 60))
            qs.append({"id": qid, "t": label, "target": target, "prog": min(prog, target), "done": bool(doneday)})
        quests[w.isoformat()] = qs
        w += dt.timedelta(days=7)
    # Staffelstab: einer hört auf, der/die andere legt innerhalb von 2 Std los → Bonus (max. 1× pro Tag)
    relay_days = set()
    if pa and pa.get("sess"):
        theirs_ = [(int(s2), int(s2) + int(sec2)) for _, s2, sec2 in pa["sess"]]
        mine_ = [(int(s0), int(s0) + int(sec)) for _, s0, sec in sess]
        def handoffs(a_list, b_list):
            for a0, a1 in a_list:
                for b0, b1 in b_list:
                    if a1 <= b0 <= a1 + 7200 and not any(x0 < b0 < x1 for x0, x1 in a_list):
                        yield b0
        for t in list(handoffs(mine_, theirs_)) + list(handoffs(theirs_, mine_)):
            d_ = (dt.datetime.fromtimestamp(t) - dt.timedelta(hours=5)).date().isoformat()
            if d_ in daylog:
                relay_days.add(d_)
        for d_ in sorted(relay_days):
            rewards.append((d_, "relay", 40))
            daylog[d_]["relay"] = 1
    # Duo-Wochenaufgabe: eine gemeinsame Aufgabe pro Woche
    duo_q = {}
    if pa:
        pw = {w_: m_ for w_, m_ in pa.get("weeks") or []}
        w = monday
        while w <= today:
            dl = [x for x in ((w + dt.timedelta(days=i)).isoformat() for i in range(7)) if x in daylog]
            seed = int(hashlib.sha256(("duo" + w.isoformat()).encode()).hexdigest(), 16)
            qid, label, target = PET_DUO_QUESTS[seed % len(PET_DUO_QUESTS)]
            prog, doneday = 0, None
            for x in dl:
                if qid == "eve2":
                    prog += daylog[x].get("evening", 0)
                elif qid == "relay3":
                    prog += daylog[x].get("relay", 0)
                else:
                    prog += daylog[x]["min"]
                    if x == dl[-1]:
                        prog += pw.get(w.isoformat(), 0)
                if prog >= target and not doneday:
                    doneday = x
            if doneday:
                rewards.append((doneday, "duo", 120))
            duo_q[w.isoformat()] = {"id": qid, "t": label, "target": target, "prog": min(prog, target), "done": bool(doneday)}
            w += dt.timedelta(days=7)
    # 3) Erfahrung je Begleiter, Schlüpfen und Entwicklung mit Datum
    rew_by = {}
    for day, why, x in rewards:
        rew_by.setdefault(day, []).append((why, x))
    base_xp = {k: int(v) for k, v in (raw.get("base") or {}).items() if k in sp_of}
    xp = {p["id"]: base_xp.get(p["id"], 0) for p in pets}
    pdays = {p["id"]: 0 for p in pets}
    hatch_day, stage_day = {}, {}
    for pid in base_xp:   # nach dem Neustart des Begleiter-Spiels schon geschlüpft
        hatch_day[pid] = epoch.isoformat() if epoch else pstart[pid]
        pdays[pid] = PET_HATCH_DAYS
    for p in pets:
        memories.append({"d": p["start"], "i": "🥚" if p["id"] not in base_xp else "🌱",
                         "t": f"{PET_SPECIES[p['sp']]['name']}-Ei ausgesucht" if p["id"] not in base_xp else f"Neustart mit {PET_SPECIES[p['sp']]['name']}"})
    last_hatched = None
    imprint = {}
    titles_by_id = {g["id"]: g["title"] for g in load_cache().get("games", [])}
    for iso in sorted(daylog):
        L = daylog[iso]
        for spid, x in L["split"].items():
            if spid not in xp or iso < pstart[spid]:
                continue
            if spid in hatch_day:
                # Erfahrung erst nach dem Schlüpfen; bis Level 10 doppelt (Startschub für neue Tiere)
                xp[spid] += x["xp"] * (2 if pet_level(xp[spid]) < PET_STAGE_LV[0] else 1)
            elif x["min"] >= 15:
                pdays[spid] += 1             # Ei: zählt nur Spieltage, an denen es dabei war
                if pdays[spid] >= PET_HATCH_DAYS:
                    hatch_day[spid] = iso
                    # Prägung: das Spiel, das am Schlupftag am meisten lief, bleibt für immer „sein“ Spiel
                    per_ = days.get(iso, {}).get("per") or {}
                    if per_:
                        top = max(per_, key=per_.get)
                        imprint[spid] = titles_by_id.get(top) or (gi.get(top) or {}).get("k")
                    memories.append({"d": iso, "i": "🐣", "t": f"{PET_SPECIES[sp_of[spid]]['stages'][0]} ist geschlüpft"
                                     + (f" – geprägt von {imprint[spid]}" if imprint.get(spid) else "")})
        day_xp = sum(x["xp"] for x in L["split"].values())
        if day_xp:   # Tiere im Nest wachsen mit 10 % mit (nur geschlüpfte, nicht die gerade aktiven)
            for spid in hatch_day:
                if spid not in L["split"] and spid in xp and iso >= pstart[spid] and iso > hatch_day[spid]:
                    xp[spid] += day_xp // 10
        pid = L["pid"]
        if pid in hatch_day:
            last_hatched = pid
        gain = sum(x for _, x in rew_by.get(iso, []))
        tgt = pid if pid in hatch_day else last_hatched   # Belohnungen für Eier bekommt der zuletzt aktive Begleiter
        if gain and tgt:
            xp[tgt] += gain
        for spid in L["split"]:
            if spid in hatch_day and spid in xp:
                lv = pet_level(xp[spid])
                for si, need in ((2, PET_STAGE_LV[0]), (3, PET_STAGE_LV[1])):
                    if lv >= need and (spid, si) not in stage_day:
                        stage_day[(spid, si)] = iso
                        memories.append({"d": iso, "i": "✨", "t": f"Entwickelt zu {PET_SPECIES[sp_of[spid]]['stages'][si - 1]}"})
    for at, k, st in completions:
        memories.append({"d": at[:10], "i": "🏁", "t": f"{titles.get(k, k)} {'durchgespielt' if st == 'durchgespielt' else 'zu 100 % geschafft'}"})
    for l in loot:
        nm = PET_ACC.get(l["val"]) if l["kind"] == "acc" else PET_HAB.get(l["val"]) if l["kind"] == "hab" else None
        if nm:
            memories.append({"d": l["d"], "i": "🎁", "t": f"Neu: {nm}" + (f" ({l['why']})" if l["why"].startswith("Geschenk") else "")})
    memories.sort(key=lambda x: x["d"], reverse=True)
    out = []
    accs = raw.get("acc") or {}
    accs2 = raw.get("acc2") or {}
    hwm = kv_get("pet_hwm", {}) or {}
    hwm_old = dict(hwm)
    for p in pets:
        hk = f"{raw.get('epoch') or ''}|{p['id']}"
        old_x, old_h = (hwm.get(hk) or [0, 0])
        total = max(xp.get(p["id"], 0), old_x)       # Fortschritt geht nie zurück, auch wenn Nachträge Tage verschieben
        pdays[p["id"]] = max(pdays.get(p["id"], 0), old_h)
        if pdays[p["id"]] >= PET_HATCH_DAYS and p["id"] not in hatch_day:
            hatch_day[p["id"]] = T
        hwm[hk] = [total, min(pdays[p["id"]], PET_HATCH_DAYS)]
        hatched = p["id"] in hatch_day
        lv = pet_level(total) if hatched else 1
        stage = 0 if not hatched else 1 if lv < PET_STAGE_LV[0] else 2 if lv < PET_STAGE_LV[1] else 3
        S = PET_SPECIES.get(p["sp"], PET_SPECIES["brasu"])
        shiny = (int(hashlib.sha256(p["id"].encode()).hexdigest(), 16) % 10 == 0) or p["id"] in got["shiny"]
        out.append({"id": p["id"], "sp": p["sp"], "species": S["name"], "nick": p.get("nick") or "", "stage": stage,
                    "stage_name": "Ei" if stage == 0 else S["stages"][stage - 1], "lvl": lv, "xp": total,
                    "xp_lv": pet_xp_for(lv), "xp_next": pet_xp_for(lv + 1), "hatch": min(pdays.get(p["id"], 0), PET_HATCH_DAYS),
                    "hatch_need": PET_HATCH_DAYS, "fav": S["fav"], "shiny": shiny, "acc": accs.get(p["id"]) if accs.get(p["id"]) in got["acc"] else None,
                    "acc2": accs2.get(p["id"]) if accs2.get(p["id"]) in got["acc"] else None,
                    "start": p["start"], "next_stage": PET_STAGE_LV[0] if stage <= 1 else PET_STAGE_LV[1] if stage == 2 else None,
                    "next_name": S["stages"][stage] if stage < 3 else None, "imprint": imprint.get(p["id"]),
                    "stars": sum(1 for x in PET_STARS if lv >= x), "next_star": next((x for x in PET_STARS if lv < x), None) if stage == 3 else None,
                    "aura": stage == 3,
                    "rare": PET_RARITY.get(p["sp"], 1)})
    if hwm != hwm_old:
        kv_set("pet_hwm", hwm)
    td = daylog.get(T, {})
    party = None
    if completions:
        at, k, st = completions[-1]
        try:
            age = time.time() - dt.datetime.fromisoformat(at.replace("Z", "+00:00")[:25]).timestamp()
        except ValueError:
            age = 1e9
        if age < 86400:
            party = {"title": titles.get(k, k), "full": st == "komplett"}
    gifts = [g for g in raw.get("gifts") or [] if isinstance(g, dict)]
    last_gift = max([str(g.get("at") or "") for g in gifts] or [""])
    res = {"pets": out, "active": active_on(T), "hab": raw.get("hab") if raw.get("hab") in got["hab"] else "lichtung",
           "unlocked": got, "loot": loot[-40:], "wins": wins, "raid_wins": raid_wins, "current": current,
           "history": history[-12:], "today": {"xp": td.get("xp", 0) + sum(x for _, x in rew_by.get(T, [])), "min": td.get("min", 0),
           "rested": bool(td.get("rested")), "cap": PET_DAY_CAP, "parts": td.get("parts") or {},
           "rewards": [{"why": w_, "xp": x} for w_, x in rew_by.get(T, [])]},
           "next_loot": next(([n, PET_LOOT[n][0], PET_LOOT[n][1]] for n in sorted(PET_LOOT) if n > wins), None),
           "starter": [k for k, v in PET_SPECIES.items() if v["start"]], "has_partner": bool(pa),
           "eggs_ready": [e for e in got["egg"] if e not in set(sp_of.values())],
           "quests": quests.get((today - dt.timedelta(days=today.weekday())).isoformat(), []),
           "duo_quest": duo_q.get((today - dt.timedelta(days=today.weekday())).isoformat()), "relay_today": T in relay_days,
           "week": (lambda w0: {"min": sum(daylog[x]["min"] for x in daylog if x >= w0), "xp": sum(daylog[x]["xp"] for x in daylog if x >= w0)
                                + sum(v for d_, _, v in rewards if d_ >= w0),
                                "won": list(dict.fromkeys(b["name"] for b in history + list(current.values()) if b.get("won") and (b.get("win_day") or "") >= w0)),
                                "quests": sum(1 for q in quests.get(w0, []) if q["done"])})((today - dt.timedelta(days=today.weekday())).isoformat()),
           "memories": memories[:60], "party": party, "album": sorted({f"{sp_of[pid]}_{s}" for (pid, s) in stage_day} | {f"{sp_of[pid]}_1" for pid in hatch_day}),
           "gift_ok": not last_gift or (today - dt.date.fromisoformat(last_gift[:10])).days >= 7, "gifts_sent": gifts[-10:]}
    _pet_cache.update(t=time.time(), v=res)
    return res


def pet_public(cfg):
    """Kurzfassung des aktiven Begleiters für den Partner (plus Geschenke an ihn)."""
    try:
        st = pet_state(cfg)
        p = next((x for x in st.get("pets") or [] if x["id"] == st.get("active")), None)
        raw = kv_get("pet", {}) or {}
        out = {k: p.get(k) for k in ("sp", "stage", "stage_name", "lvl", "nick", "shiny", "acc", "acc2", "species", "aura", "stars")} if p else {}
        out["gifts"] = [g for g in raw.get("gifts") or [] if isinstance(g, dict)][-20:]
        return out if (p or out["gifts"]) else None
    except Exception:
        return None


def check_pet(cfg):
    """Pushes: geschlüpft, Entwicklung, alle 5 Level, Boss besiegt, neue Beute, Geschenke."""
    st = pet_state(cfg, force=True)
    if not st.get("pets"):
        return
    seen = kv_get("pet_seen", None)
    now = {"p": {p["id"]: [p["stage"], p["lvl"]] for p in st["pets"]}, "wins": st["wins"] + st["raid_wins"], "loot": len(st["loot"])}
    if seen is None:
        kv_set("pet_seen", now)
        return
    for p in st["pets"]:
        old = (seen.get("p") or {}).get(p["id"], [0, 1])
        name = p["nick"] or p["stage_name"]
        if p["stage"] > old[0]:
            if p["stage"] == 1:
                alert("pet", "🐣 Dein Ei ist geschlüpft!", f"Sag Hallo zu {name} – tippe, um es zu sehen.", "#/begleiter")
            else:
                alert("pet", f"✨ {p['species']} hat sich entwickelt!", f"Aus deinem Begleiter wurde {p['stage_name']} (Level {p['lvl']}).", "#/begleiter")
        elif p["stage"] and p["lvl"] // 5 > old[1] // 5:
            alert("pet", f"⬆️ {name} ist jetzt Level {p['lvl']}", "Weiter so!", "#/begleiter")
    if now["wins"] > (seen.get("wins") or 0):
        last = (st.get("history") or [{}])[-1]
        alert("pet", f"⚔️ {last.get('name', 'Boss')} besiegt!", "Dein Begleiter hat gewonnen – schau dir die Beute an.", "#/begleiter")
    for l in st["loot"][(seen.get("loot") or 0):] if len(st["loot"]) > (seen.get("loot") or 0) else []:
        if l["why"].startswith("Geschenk"):
            alert("pet", f"🎁 {l['why']}", f"{PET_ACC.get(l['val'], l['val'])} für deinen Begleiter 💞", "#/begleiter")
    kv_set("pet_seen", now)
    pet_weekly_push(st)


def pet_weekly_push(st, now=None):
    """Sonntagabend: kurzer Wochenrückblick vom Begleiter (einmal pro Woche)."""
    now = now or dt.datetime.now()
    if now.weekday() != 6 or now.hour < 19:
        return None
    wk = (now.date() - dt.timedelta(days=6)).isoformat()
    if kv_get("pet_weekly_sent") == wk:
        return None
    p = next((x for x in st.get("pets") or [] if x["id"] == st.get("active")), None)
    if not p or not p["stage"]:
        return None
    if planned_done(f"gr-petweek-{wk}"):    # schon vorab eingeplant und zugestellt
        kv_set("pet_weekly_sent", wk)
        return None
    name = p["nick"] or p["stage_name"]
    body = pet_weekly_text(st, p)
    kv_set("pet_weekly_sent", wk)
    alert("pet", f"🐾 Wochenrückblick von {name}", body, "#/begleiter")
    return body


def pet_weekly_text(st, p):
    w = st.get("week") or {}
    parts = [f"{w.get('min', 0) // 60} Std {w.get('min', 0) % 60} Min zusammen gespielt" if w.get("min") else "eine ruhige Woche"]
    if w.get("xp"):
        parts.append(f"+{w['xp']} Erfahrung (jetzt Level {p['lvl']})")
    if w.get("won"):
        parts.append(" und ".join(w["won"][:2]) + " besiegt")
    if w.get("quests"):
        parts.append(f"{w['quests']} Aufgabe{'n' if w['quests'] > 1 else ''} geschafft")
    return "Diese Woche: " + ", ".join(parts) + ". Bis nächste Woche! 💛"


def duo_world():
    """Was der Partner von „unserer Welt“ sieht: Türchen, Momente, Wünsche, Überraschungen (erst ab Termin), Zeitkapsel (erst ab Öffnung)."""
    now = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
    notes = kv_get("snotes", {}) or {}
    sur = []
    for x in kv_get("surprises", []) or []:
        open_ = x["reveal"] <= now
        sur.append({"id": x["id"], "reveal": x["reveal"], "at": x["at"], "open": open_,
                    "title": x["title"] if open_ else "", "text": x["text"] if open_ else ""})
    cap = kv_get("capsule", {}) or {}
    capo = {"sealed": True, "open": cap["open"], "at": cap["at"],
            **({"text": cap["text"], "sealed": False} if cap["open"] <= now[:10] else {})} if cap else None
    adv = kv_get("advent", {}) or {}
    pa_ad = set(((kv_get("partner", None) or {}).get("advent_ad")) or [])
    ans = adv.get("a") or {}
    return {"advent": sorted(adv.get("o") or {})[-30:],
            "advent_ad": sorted(ans)[-30:],                                   # an welchen Tagen ich geantwortet habe
            "advent_a": {k: x for k, x in ans.items() if k in pa_ad},         # Antworten erst, wenn beide geantwortet haben
            "advent_v": adv.get("v") or {},
            "visits": (kv_get("visits", []) or [])[-30:], "dates": kv_get("dates", {}) or {},
            "push_off": [k for k, x in (((kv_get("fx", {}) or {}).get("push")) or {}).items() if x is False],
            "notes": dict(list(((k, x) for k, x in notes.items() if x[0] or x[1]))[-150:]),
            "wishes": kv_get("wishes", []) or [], "wish_done": kv_get("wish_done", {}) or {},
            "surprises": sur[-20:], "capsule": capo}


def duo_extras(cfg, games, keys):
    """Zusatzdaten für „Wir zwei“: Avatar, Wochen, Genres, Tier-Liste, gerade gespielt."""
    sx = read_json(STEAMX_PATH, {}) or {}
    gen = {}
    for g in games:
        for n in g.get("genres") or []:
            gen[n] = gen.get(n, 0) + int(g.get("minutes") or 0)
    meta = load_meta()
    tiers = {k: v["tier"] for k, v in meta.items() if v.get("tier") and k in keys}
    playing = [{"k": norm_title(p.get("title") or ""), "t": p.get("title"), "s": p.get("since")} for p in (TRACKER.current or [])
               if not is_non_game(p.get("title")) and not (meta.get(norm_title(p.get("title") or "")) or {}).get("hide")]
    since = int(time.time()) - 400 * 86400
    sess = [[k, int(st), int(sec)] for k, st, sec in tracker.load_sessions(APP_DB, since)]
    return {"avatar": (sx.get("me") or {}).get("avatar"), "weeks": weekly_minutes(), "months": monthly_minutes(),
            "genres": dict(sorted(gen.items(), key=lambda x: -x[1])[:16]), "tiers": tiers, "playing": playing,
            "sess": sess[-1500:], "news": duo_news(games, meta), "topic": (cfg.get("push_topic") or "").strip() or None,
            "pet": pet_public(cfg), "plans": kv_get("plans", []) or [], "plan_resp": kv_get("plan_resp", {}) or {},
            "plan_pushed": list((kv_get("plan_pushed", {}) or {}))[-120:],
            **duo_world(),
            "wake": pcutil.mac_address() if cfg.get("allow_wake") else None}


def duo_news(games, meta):
    """Neuigkeiten für den Partner: durchgespielt, neu in der Bibliothek, S-/A-Tier. Neueste zuerst."""
    titles = {norm_title(g["title"]): g["title"] for g in games}
    ev = []
    for k, st, at in load_status_log(120):
        if st in ("durchgespielt", "komplett") and k in titles:
            ev.append(["done" if st == "durchgespielt" else "full", k, titles[k], at])
    # neu gekauft: nur echte Neuzugänge (keine Massen-Importe, nicht vor Beginn der Aufzeichnung)
    start = first_tracking_day() or ""
    per_day = {}
    for g in games:
        a = str(g.get("added") or "")[:10]
        if a:
            per_day[a] = per_day.get(a, 0) + 1
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    seen = set()
    for g in games:
        a = str(g.get("added") or "")
        k = norm_title(g["title"])
        if not a or a[:10] <= start or a[:10] < cutoff or per_day.get(a[:10], 0) > 8 or g.get("family") or k in seen:
            continue
        seen.add(k)
        ev.append(["new", k, g["title"], a])
    for k, v in meta.items():
        if v.get("tier") in ("S", "A") and v.get("updated") and str(v["updated"])[:10] >= cutoff and k in titles:
            ev.append(["tier" + v["tier"], k, titles[k], v["updated"]])
    ev.sort(key=lambda e: str(e[3]), reverse=True)
    return ev[:30]


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
        e = groups.setdefault(k, {"t": g["title"], "m": 0, "y": 0, "mo": 0, "own": False, "c": None, "mp": False, "d": set(), "a": None})
        if g.get("steam_appid") and not e["a"]:
            e["a"] = str(g["steam_appid"])
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
        rows.append([k, e["t"], e["m"], e["y"], e["mo"], int(e["own"]), e["c"] or xc.get(k), int(e["mp"]), sorted(e["d"]), e["a"]])
    out = {"v": 1, "name": (cfg.get("user_name") or "").strip() or "Partner", "year_n": dt.date.today().year,
           "total": sum(r[2] for r in rows), "year": sum(r[3] for r in rows), "month": sum(r[4] for r in rows), "games": rows}
    out.update(duo_extras(cfg, games, set(groups)))
    if not stable:
        out["at"] = now_iso()
    elif out["playing"]:
        out["hb"] = int(time.time() // 600)  # beim Spielen alle 10 Min. neu hochladen, damit „spielt gerade“ frisch bleibt
    return out


# ================================================================ PC-Fernbedienung, Downloads, Speicherplatz

pcstat = {"t": 0, "sizes_t": 0, "v": {}}


def pc_status_worker(cfg, force=False):
    """Downloads (Steam), freier Speicher und große, lange nicht gespielte Spiele – für Handy und Start-Seite."""
    if not force and time.time() - pcstat["t"] < (25 if (pcstat.get("v") or {}).get("downloads") else 90):
        return pcstat["v"]
    pcstat["t"] = time.time()
    games = load_cache().get("games", [])
    try:
        apps = pcutil.steam_apps()
    except Exception:
        apps = []
    dls = []
    for a in apps:
        if not a["downloading"]:
            continue
        done = a["done"]
        if a["total"] and done < a["total"]:   # Steam schreibt den Stand nur selten – der Download-Ordner wächst laufend
            st = pcutil.folder_size(os.path.join(a["lib"], "steamapps", "downloading", str(a["appid"])), limit_s=2) or 0
            done = max(done, min(st, a["total"]))
        dls.append({"name": a["name"], "pct": int(done * 100 / a["total"]) if a["total"] else 0, "gb": round(a["total"] / 1e9, 1),
                    "appid": a["appid"], "paused": not a["state"] & 1024})
    # Größen: Steam aus den Manifesten, andere Launcher einmal am Tag über den Ordner
    sizes = read_json(os.path.join(DATA_DIR, "sizes.json"), {}) if os.path.exists(os.path.join(DATA_DIR, "sizes.json")) else {}
    steam_size = {str(a["appid"]): a["size"] for a in apps if a["size"]}
    if time.time() - pcstat["sizes_t"] > 86400 or not sizes:
        pcstat["sizes_t"] = time.time()
        for g in games:
            d = g.get("install_dir")
            if g.get("installed") and d and not g.get("steam_appid") and os.path.isdir(d) and (time.time() - (sizes.get(d) or [0, 0])[1] > 7 * 86400):
                sz = pcutil.folder_size(d, limit_s=8)
                if sz:
                    sizes[d] = [sz, time.time()]
        write_json(os.path.join(DATA_DIR, "sizes.json"), sizes)
    meta = load_meta()
    cands = []
    for g in games:
        if g.get("device") != "PC" or not g.get("installed"):
            continue
        b = steam_size.get(str(g.get("steam_appid") or "")) or (sizes.get(g.get("install_dir") or "") or [0])[0]
        if not b or b < 3e9:
            continue
        lp = parse_iso(g.get("last_played"))
        idle = int((time.time() - lp) / 86400) if lp else None
        st = (meta.get(norm_title(g["title"])) or {}).get("status")
        done = st in ("durchgespielt", "komplett", "abgebrochen")
        if done or idle is None or idle >= 90:
            cands.append({"t": g["title"], "gb": round(b / 1e9, 1), "idle": idle, "done": done, "appid": g.get("steam_appid"),
                          "drive": os.path.splitdrive(g.get("install_dir") or "")[0]})
    cands.sort(key=lambda c: -(c["gb"] * (2 if c["done"] else 1) * (1 + (c["idle"] or 0) / 180)))
    paths = [g.get("install_dir") for g in games if g.get("installed") and g.get("install_dir")] + [a["lib"] for a in apps]
    disks = [{"d": d.rstrip("\\/") or d, "free": round(f / 1e9), "total": round(t / 1e9)} for d, (f, t) in pcutil.drives(paths).items()]
    pcstat["v"] = {"at": now_iso(), "downloads": dls, "space": cands[:12], "disks": disks,
                   "timer": kv_get("pc_timer", None), "can_wake": bool((kv_get("partner", None) or {}).get("wake"))}
    return pcstat["v"]


def handle_remote(cfg):
    """Befehle vom Handy ausführen (Gute-Nacht-Timer, Ruhezustand, Partner-PC aufwecken) und Timer überwachen."""
    r = kv_get("remote", None) or {}
    if r.get("id") and r["id"] != kv_get("remote_done"):
        kv_set("remote_done", r["id"])
        if time.time() - (r.get("ts") or 0) / 1000 < 3600:     # alte Befehle nicht nachträglich ausführen
            remote_exec(cfg, r["cmd"], r.get("arg"))
    t = kv_get("pc_timer", None)
    if not t:
        return
    now = dt.datetime.now()
    fire = False
    if t.get("mode") == "after_session":
        if TRACKER.current:
            t["seen_play"] = True
            kv_set("pc_timer", t)
        elif t.get("seen_play") or time.time() - t.get("set", 0) > 1800:
            fire = True    # Session vorbei (oder 30 Min nichts gestartet)
    elif t.get("mode") == "at_time":
        fire = now.strftime("%Y-%m-%dT%H:%M") >= t.get("when", "9999")
    if fire:
        kv_set("pc_timer", None)
        try:
            remember_night()
        except Exception as e:
            print("  Abend merken:", e)
        try:
            pcutil.power("shutdown", 120, "Gaming Recap: Gute Nacht! Der PC fährt in 2 Minuten herunter.")
            alert("pause", "🌙 Gute Nacht", "Dein PC fährt in 2 Minuten herunter. Abbrechen: in der App „Timer stoppen“.")
        except Exception as e:
            print("  Herunterfahren:", e)


def remember_night():
    """„Licht aus“: kurzer Rückblick auf den Abend für den Guten-Morgen-Gruß."""
    mid = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1 if dt.datetime.now().hour < 5 else 0), dt.time(5))
    per = {}
    for k, st, sec in tracker.load_sessions(APP_DB, int(mid.timestamp())):
        per[k] = per.get(k, 0) + int(sec)
    top = max(per.items(), key=lambda x: x[1])[0] if per else None
    kv_set("last_night", {"at": now_iso(), "min": sum(per.values()) // 60, "top": top, "games": len(per)})
    cloud_state["pending"] = cloud_state["urgent"] = True


def remote_exec(cfg, cmd, arg=None):
    now = dt.datetime.now()
    if cmd == "after_session":
        kv_set("pc_timer", {"mode": "after_session", "set": time.time(), "seen_play": bool(TRACKER.current)})
    elif cmd == "at_time" and re.fullmatch(r"\d{2}:\d{2}", arg or ""):
        t = now.replace(hour=int(arg[:2]), minute=int(arg[3:]), second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
        kv_set("pc_timer", {"mode": "at_time", "when": t.strftime("%Y-%m-%dT%H:%M"), "set": time.time()})
    elif cmd == "cancel":
        kv_set("pc_timer", None)
        try:
            pcutil.power("abort")
        except Exception:
            pass
    elif cmd == "sleep":
        try:
            remember_night()
        except Exception:
            pass
        pcutil.power("sleep")
    elif cmd == "shutdown":
        pcutil.power("shutdown", 60)
    elif cmd == "wake_partner":
        mac = (kv_get("partner", None) or {}).get("wake")
        if mac:
            pcutil.wake(mac)
    pcstat["t"] = 0
    cloud_state["pending"] = True
    return True


def year_ago():
    """„Heute vor einem Jahr“: was du an diesem Tag gespielt und geschafft hast."""
    d = dt.date.today()
    try:
        y = d.replace(year=d.year - 1)
    except ValueError:
        y = d - dt.timedelta(days=365)
    titles = {g["id"]: g["title"] for g in load_cache().get("games", [])}
    con = history_con()
    a = dict(con.execute("SELECT id, minutes FROM snapshots WHERE day = (SELECT MAX(day) FROM snapshots WHERE day < ?)", (y.isoformat(),)).fetchall())
    b = dict(con.execute("SELECT id, minutes FROM snapshots WHERE day = ?", (y.isoformat(),)).fetchall())
    con.close()
    games = sorted(((titles.get(k, k), m - a.get(k, m)) for k, m in b.items() if k in a and m - a[k] >= 15), key=lambda x: -x[1])[:3]
    tn = {norm_title(t): t for t in titles.values()}
    done = [tn.get(k, k) for k, st, at in load_status_log(800) if str(at)[:10] == y.isoformat() and st in ("durchgespielt", "komplett")]
    ss = [s_ for s_ in tracker.load_sessions(APP_DB, int(dt.datetime.combine(y, dt.time()).timestamp()))
          if dt.date.fromtimestamp(s_[1]) == y]
    if not ss:
        pass
    for k, st, sec in ss:
        t = tn.get(k, k)
        if t not in [g[0] for g in games] and sec >= 900:
            games.append((t, sec // 60))
    if not games and not done:
        return None
    return {"date": y.isoformat(), "games": [[t, int(m)] for t, m in games[:3]], "done": done[:3]}


def countdowns():
    """Erscheinungstermine von der Wunschliste (die nächsten 6)."""
    sx = read_json(STEAMX_PATH, {}) or {}
    now = time.time()
    out = []
    for w in sx.get("wishlist") or []:
        p = (sx.get("prices") or {}).get(str(w.get("appid"))) or {}
        rel = p.get("release")
        if rel and now < rel < now + 400 * 86400:
            out.append({"name": p.get("name") or "?", "at": int(rel), "appid": w.get("appid")})
    return sorted(out, key=lambda x: x["at"])[:6]


def duo_plan_alert(cfg, pa):
    """Neue Zockabend-Vorschläge oder Zusagen des Partners einmal melden (falls das Handy sie nicht schon direkt geschickt hat)."""
    seen = kv_get("plan_seen", {}) or {}
    name = pa.get("name") or "Dein Partner"
    changed = False
    for p in pa.get("plans") or []:
        if p["id"] not in seen and not p.get("cancel") and p["at"] >= dt.datetime.now().strftime("%Y-%m-%dT%H:%M"):
            seen[p["id"]] = "new"
            changed = True
            if "p:" + p["id"] not in (pa.get("plan_pushed") or []) and time.time() - (p.get("created") or 0) > 900:
                alert("duo", f"🎮 {name} schlägt einen Zockabend vor", plan_text(p))   # nur falls kein direkter Push kam
    mine = {p["id"]: p for p in kv_get("plans", []) or []}
    for pid, r in (pa.get("plan_resp") or {}).items():
        k = "r:" + pid
        if pid in mine and seen.get(k) != r:
            seen[k] = r
            changed = True
    if changed:
        kv_set("plan_seen", dict(list(seen.items())[-200:]))


PLAN_KEYS = ("plans", "plan_resp", "plan_pushed", "remote")


def plan_push_out(cfg):
    """Zockabend: Vorschläge, Absagen und Antworten sofort per Push an den Partner (vom PC aus, zuverlässig).
    Was das Handy schon selbst geschickt hat, steht in plan_pushed und wird nicht doppelt geschickt."""
    with plan_lock:
        pa = kv_get("partner", None) or {}
        topic = pa.get("topic")
        pushed = kv_get("plan_pushed", None)
        mine, resp = kv_get("plans", []) or [], kv_get("plan_resp", {}) or {}
        now = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
        todo = []
        for p in mine:
            todo.append(("p:" + p["id"], p, "new"))
            if p.get("cancel"):
                todo.append(("c:" + p["id"], p, "cancel"))
        theirs = {p["id"]: p for p in pa.get("plans") or []}
        for pid, r in resp.items():
            todo.append((f"r:{pid}:{r}", theirs.get(pid), r))
        if pushed is None:   # erster Lauf: Bestehendes gilt als verschickt
            kv_set("plan_pushed", {k: int(time.time()) for k, _, _ in todo})
            return 0
        me = (cfg.get("user_name") or "").strip() or "Dein Player 2"
        pu = (cfg.get("partner_url") or "").strip()
        click = (("https://" + pu if not pu.startswith("http") else pu).rstrip("/") + "/#/uebersicht") if pu else None   # öffnet beim Partner dessen App
        n = 0
        for key, p, what in todo:
            if key in pushed:
                continue
            pushed[key] = int(time.time())
            if not topic or not p or p.get("at", "") < now:
                continue
            if what == "new" and not p.get("cancel"):
                notify.push(f"🎮 {me} schlägt einen Zockabend vor", plan_text(p), url=click, topic=topic, tags="gr-plan", sid=f"gr-plan-{p['id']}")
            elif what == "cancel":
                notify.push(f"😢 {me} muss absagen", plan_text(p) + " fällt leider aus.", url=click, topic=topic, tags="gr-plan", sid=f"gr-plan-{p['id']}")
            elif what in ("yes", "maybe", "no"):
                t = {"yes": f"🎉 {me} ist dabei!", "maybe": f"🤔 {me} weiß noch nicht", "no": f"😢 {me} kann leider nicht"}[what]
                notify.push(t, plan_text(p), url=click, topic=topic, tags="gr-plan", sid=f"gr-resp-{p['id']}")
            n += 1
        kv_set("plan_pushed", dict(list(pushed.items())[-300:]))
        if n:
            cloud_state["pending"] = cloud_state["urgent"] = True
        return n


plan_lock = threading.Lock()


def plan_text(p):
    try:
        t = dt.datetime.fromisoformat(p["at"])
        days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        when = f"{days[t.weekday()]} {t.strftime('%d.%m.')} um {t.strftime('%H:%M')} Uhr"
    except ValueError:
        when = p["at"]
    return when + (f" · {p['game']}" if p.get("game") else "") + (f" – {p['note']}" if p.get("note") else "")


def partner_every():
    """Wie oft die Partner-Daten geholt werden: normal jede Minute, nach einem Zockabend-Push 3 Min. lang alle 20 Sek."""
    return 20 if time.time() < partner_state.get("burst", 0) else 60


def fetch_partner(cfg):
    """Holt die Daten des Partners (verschlüsselt in dessen GitHub-Pages-Repository)."""
    url, code = (cfg.get("partner_url") or "").strip(), (cfg.get("partner_code") or "").strip()
    if not url or not code or time.time() - partner_state["last"] < partner_every() - 5:
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
        duo_live_alert(cfg, data)
        duo_news_alert(data)
        duo_plan_alert(cfg, data)
    except urllib.error.HTTPError as e:
        partner_state["error"] = "Beim Partner liegt noch nichts (sein PC muss einmal hochgeladen haben)." if e.code == 404 else err_text(e)
    except ValueError:
        partner_state["error"] = "Partner-Code passt nicht – bei beiden muss derselbe Code eingetragen sein."
    except Exception as e:
        partner_state["error"] = err_text(e)


_me_duo = {"t": 0, "v": None}


def me_duo(cfg):
    """Eigene Wir-zwei-Zusatzdaten fürs Frontend (Wochen sind teuer, daher 10 Min. zwischengespeichert)."""
    if time.time() - _me_duo["t"] > 600 or _me_duo["v"] is None:
        d = partner_summary(cfg, stable=True)
        for x in ("games", "hb", "sess", "topic"):
            d.pop(x, None)
        _me_duo["v"] = d
        _me_duo["t"] = time.time()
    return _me_duo["v"]


def poll_greets(cfg):
    """Grüße vom Partner kommen über das eigene ntfy-Thema (sofort als Push aufs Handy).
    Der PC holt sie zusätzlich ab, zeigt sie in der App und als Windows-Hinweis."""
    topic = (cfg.get("push_topic") or "").strip()
    if not topic or not cfg.get("partner_url"):
        return
    since = kv_get("greet_since", None) or "12h"
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}/json?poll=1&since={since}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            lines = r.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return
    greets, new, last = kv_get("greets", []) or [], [], None
    have = {g.get("id") for g in greets}
    for ln in lines:
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        if m.get("event") != "message":
            continue
        last = m.get("id") or last
        if "gr-plan" in (m.get("tags") or []) and int(m.get("time") or 0) > time.time() - 600:
            partner_state["burst"], partner_state["last"] = time.time() + 180, 0   # Partner hat geplant: Daten gleich holen
        if "gr-greet" not in (m.get("tags") or []) or m.get("id") in have:
            continue
        g = {"id": m.get("id"), "at": int(m.get("time") or time.time()), "title": str(m.get("title") or "")[:80],
             "msg": str(m.get("message") or "")[:200]}
        greets.append(g)
        new.append(g)
    if last:
        kv_set("greet_since", last)
    if new:
        kv_set("greets", greets[-30:])
        cloud_state["pending"] = True
        prefs = notify_prefs(cfg)
        if (prefs.get("cats") or {}).get("duo", True) and prefs.get("win", True):
            for g in new[-3:]:
                notify.toast(g["title"] or "💞 Gruß", g["msg"], f"http://localhost:{PORT}/#/freunde", phone=False, win=True)


def duo_live(data):
    """Was der Partner gerade spielt – nur wenn seine Daten frisch sind (sonst ist der PC aus)."""
    try:
        age = time.time() - dt.datetime.fromisoformat(str(data.get("at"))).timestamp()
    except (TypeError, ValueError):
        return []
    return data.get("playing") or [] if age < 25 * 60 else []


NEWS_TEXT = {"done": "hat {t} durchgespielt 🎉", "full": "hat {t} zu 100 % geschafft 🏆", "new": "hat {t} neu in der Bibliothek",
             "tierS": "hat {t} ins S-Tier gepackt ⭐", "tierA": "hat {t} ins A-Tier gepackt"}


def duo_news_alert(data):
    seen = kv_get("duo_news_seen", None)
    ids = [f"{e[0]}|{e[1]}|{e[3]}" for e in data.get("news") or []]
    if seen is None:  # erster Abruf: nur merken, nicht alles auf einmal melden
        kv_set("duo_news_seen", ids[:60])
        return
    fresh = [e for e, i in zip(data.get("news") or [], ids) if i not in seen]
    for e in fresh[:3]:
        try:
            age = time.time() - dt.datetime.fromisoformat(str(e[3]).replace("Z", "+00:00")[:26]).timestamp()
        except ValueError:
            age = 0
        if age < 3 * 86400 and e[0] in NEWS_TEXT:
            alert("duo", f'💞 {data.get("name") or "Partner"}', NEWS_TEXT[e[0]].format(t=e[2]), "#/freunde")
    if fresh:
        kv_set("duo_news_seen", (ids + seen)[:60])


def duo_live_alert(cfg, data):
    seen = kv_get("duo_seen", {})
    for p in duo_live(data):
        sid = f'{p.get("k")}|{p.get("s")}'
        if sid in seen:
            continue
        seen[sid] = int(time.time())
        try:
            fresh = time.time() - float(p.get("s") or 0) < 45 * 60
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            alert("duo", f'💞 {data.get("name") or "Partner"} spielt gerade', p.get("t") or "", "#/freunde", queue=False)
    if len(seen) > 40:
        seen = dict(sorted(seen.items(), key=lambda x: -x[1])[:20])
    kv_set("duo_seen", seen)


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


def backup_dir():
    return os.path.join(DATA_DIR, "sicherung")


def local_backup(tag="manuell", keep=6):
    """Sicherung auf diesem PC: Datenbanken (sauber über die SQLite-Backup-Funktion), Daten-Dateien
    (ohne config.json – Keys bleiben, wo sie sind) und die Programmdateien. Die ältesten werden aufgeräumt."""
    import sqlite3
    name = dt.datetime.now().strftime("%Y-%m-%d_%H%M") + "_" + re.sub(r"[^A-Za-z0-9_-]", "", tag)[:30]
    d = os.path.join(backup_dir(), name)
    os.makedirs(os.path.join(d, "daten"), exist_ok=True)
    for db in (HISTORY_DB, APP_DB):
        if os.path.exists(db):
            src, dst = sqlite3.connect(db), sqlite3.connect(os.path.join(d, "daten", os.path.basename(db)))
            with dst:
                src.backup(dst)
            src.close(); dst.close()
    for n in os.listdir(DATA_DIR):
        p = os.path.join(DATA_DIR, n)
        if os.path.isfile(p) and n.endswith(".json") and n != os.path.basename(CONFIG_PATH):
            shutil.copy2(p, os.path.join(d, "daten", n))
    for rel, b in updater.program_files(BASE).items():
        q = os.path.join(d, "programm", *rel.split("/"))
        os.makedirs(os.path.dirname(q), exist_ok=True)
        with open(q, "wb") as f:
            f.write(b)
    write_json(os.path.join(d, "sicherung.json"), {"at": now_iso(), "tag": tag, "version": VERSION})
    olds = sorted(n for n in os.listdir(backup_dir()) if os.path.isdir(os.path.join(backup_dir(), n)))
    for n in olds[:-keep]:
        shutil.rmtree(os.path.join(backup_dir(), n), ignore_errors=True)
    return name


def list_backups():
    out = []
    if os.path.isdir(backup_dir()):
        for n in sorted(os.listdir(backup_dir()), reverse=True):
            p = os.path.join(backup_dir(), n)
            if os.path.isdir(p):
                size = sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(p) for f in fs)
                out.append({"name": n, "mb": round(size / 1e6, 1)})
    return out


def restore_local(name="latest"):
    """Zurückspielen (nur bei beendetem Gaming Recap, über Zuruecksetzen.bat): Programm + Daten aus der Sicherung."""
    names = [b["name"] for b in list_backups()]
    if not names:
        print("Keine Sicherung gefunden.")
        return False
    name = names[0] if name in ("latest", "", None) else name
    d = os.path.join(backup_dir(), name)
    if not os.path.isdir(d):
        print("Sicherung nicht gefunden:", name)
        return False
    local_backup("vor-zuruecksetzen")   # auch den aktuellen Stand nicht verlieren
    for root in ("programm", "daten"):
        base = os.path.join(d, root)
        for a, _, fs in os.walk(base):
            for f in fs:
                rel = os.path.relpath(os.path.join(a, f), base)
                dst = os.path.join(BASE if root == "programm" else DATA_DIR, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(a, f), dst)
    print("Zurückgesetzt auf", name)
    return True


def just_updated():
    """Wurde in den letzten 20 Minuten ein Update eingespielt?"""
    try:
        return time.time() - os.path.getmtime(os.path.join(DATA_DIR, "app_sicherung", "dateien.json")) < 1200
    except OSError:
        return False


def update_worker(cfg, force=False, apply_now=None):
    """Einmal am Tag nach Updates schauen und (falls eingeschaltet) installieren."""
    url = update_source(cfg)
    if not url or cfg.get("publish_updates") or update_state["checking"]:
        return
    gap = 120 if just_updated() else 3 * 3600   # direkt nach einem Update kurz nachfassen (z. B. neu hinzugekommene Dateien)
    if not force and time.time() - update_state["last_check"] < gap and not update_state.get("deferred"):
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
        try:
            local_backup("vor-update")
        except Exception as e:
            print("  Sicherung vor Update:", err_text(e))
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
    sc = kv_get("selfcheck", None) or {}
    data_checks = [c for c in sc.get("checks") or [] if not c["area"].startswith(("Quelle", "Handy-Cloud"))]
    if data_checks:
        bad = [c for c in data_checks if not c["ok"]]
        if bad:
            for c in bad:
                add("Daten: " + c["area"], False, c["text"], "Sag Claude Bescheid – das sollte nie passieren.")
        else:
            add("Daten-Selbstcheck", True, f"{len(data_checks)} Prüfungen in Ordnung (Verlauf, Tier-Liste, Begleiter)")
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
            exp = cloud_state.get("token_exp")
            tok = ""
            if exp:
                try:
                    tok = " · Token gültig bis " + dt.datetime.strptime(exp.replace(" UTC", "").strip()[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
                except ValueError:
                    pass
            elif exp == "":
                tok = " · Token ohne Ablaufdatum"
            add("Handy-Cloud", True, "läuft" + (f", zuletzt {time.strftime('%H:%M', time.localtime(lp))}" if lp else "") + tok)
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
    elif cfg.get("partner_url"):
        pa_ = kv_get("partner", None) or {}
        age = time.time() - (parse_iso(pa_.get("at")) or time.time())
        if age > 36 * 3600:
            nm = pa_.get("name") or "Partner"
            add("Wir zwei", False, f"Von {nm} kommt seit {int(age // 86400)} Tag{'en' if age >= 2 * 86400 else ''} nichts mehr "
                f"(zuletzt {dt.datetime.fromtimestamp(parse_iso(pa_.get('at'))).strftime('%d.%m. %H:%M')}).",
                f"Läuft Gaming Recap auf dem PC von {nm}? Dort http://localhost:8765 öffnen – sonst Start.bat starten. "
                "Läuft es, unter Einstellungen › System › Systemcheck nach einem Fehler bei „Handy-Cloud“ schauen (GitHub-Token abgelaufen?).", "warn")
    af = autostart_file()
    if af and not os.path.exists(af) and not other_autostart():
        add("Autostart", False, "Gaming Recap startet nicht mit Windows.", "Einstellungen › Extras › „Mit Windows starten“ anhaken.", "warn")
    if not (cfg.get("push_topic") or "").strip():
        add("Push aufs Handy", False, "nicht eingerichtet", "Einstellungen › Push aufs Handy.", "info")
    elif cloud_ready(cfg):
        cn = kv_get("cloud_notify", None) or {}
        repo = (cfg.get("gh_repo") or "").strip().strip("/").replace("https://github.com/", "")
        if cloud_state.get("cn_wf_error"):
            add("Benachrichtigungen ohne PC", False, "Die GitHub Action konnte nicht angelegt werden.",
                f"Auf github.com › Settings › Developer settings › Fine-grained tokens deinen Token öffnen, bei „Repository permissions“ "
                f"„Workflows“ auf „Read and write“ stellen und speichern. Danach legt Gaming Recap sie beim nächsten Abgleich selbst an.", "warn")
        elif cloud_notify_active():
            add("Benachrichtigungen ohne PC", True, f"aktiv (Epic-Gratisspiele, Wunschlisten-Angebote · zuletzt {cn.get('last_day')})")
        elif cn.get("status") and any("fehlt" in x for x in cn["status"]) or (cn and not cn.get("last_day")):
            add("Benachrichtigungen ohne PC", False, "Noch nicht eingerichtet: " + "; ".join(cn.get("status") or []),
                f"Auf github.com/{repo} › Settings › Secrets and variables › Actions zwei „Repository secrets“ anlegen: "
                "NTFY_TOPIC = dein Push-Thema (Einstellungen › Push aufs Handy), STEAM_ID = deine Steam-ID (Einstellungen › Quellen). "
                f"Dann unter github.com/{repo}/actions „Gaming Recap Benachrichtigungen“ › „Run workflow“.", "warn")
    return out


def check_health(cfg):
    """Neue Probleme einmal melden (Windows + Handy)."""
    sc = kv_get("selfcheck", None)
    if not sc or time.time() - (parse_iso(sc.get("at")) or 0) > 3600:   # Daten-Selbstcheck stündlich
        try:
            kv_set("selfcheck", {"at": now_iso(), "checks": self_check(cfg)})
        except Exception as e:
            kv_set("selfcheck", {"at": now_iso(), "checks": [{"ok": False, "area": "Selbstcheck", "text": err_text(e)}]})
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
    wait = 86400 if cloud_state.get("token_exp") is not None else 3600   # bei Fehlern stündlich neu versuchen
    if cloud_ready(cfg) and now - (cloud_state.get("token_exp_checked") or 0) > wait:
        try:
            cloud_state["token_exp"] = cloud.GitHub(cfg["gh_repo"], cfg["gh_token"]).token_expiry() or ""   # "" = ohne Ablaufdatum
        except Exception as e:
            print("  Token-Prüfung:", err_text(e))
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
                pet = pet_public(cfg) or {}
                if pet.get("stage"):
                    alert("pause", f"🧃 {pet.get('nick') or pet.get('stage_name')}: Kurze Pause?",
                          f"Du spielst {p['title']} seit {ph:g} Stunden am Stück. Ich hol uns was zu trinken!")
                else:
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
            if rel and dt.date.fromtimestamp(rel) == today and str(w.get("appid")) not in seen and not planned_done(f"gr-rel-{w.get('appid')}"):
                seen.add(str(w.get("appid")))
                kv_set("release_seen", sorted(seen)[-300:])
                alert("release", "Heute erschienen", f"{p.get('name') or 'Ein Spiel von deiner Wunschliste'} ist ab heute erhältlich"
                             + (f" – {p.get('fmt')}" if p.get("fmt") else "") + ".",
                             f"https://store.steampowered.com/app/{w.get('appid')}")
    if cats.get("yearly", True):
        now = dt.datetime.now()
        base = cloud_state.get("url") or f"http://localhost:{PORT}/"
        if now.month == 12 and now.day <= 7 and now.hour >= 10 and kv_get("season_sent") != f"{now.year}-dec" and not planned_done(f"gr-year-{now.year}"):
            kv_set("season_sent", f"{now.year}-dec")
            pm, _ = period_minutes(load_cache().get("games", []), "year")
            h = sum((pm or {}).values()) / 60
            alert("yearly", f"🎬 Dein Gaming-Jahr {now.year} ist da",
                  (f"Bisher {h:,.0f} Stunden. ".replace(",", ".") if h else "") + "Dein Jahres-Wrapped ist fertig – tippe für deine Story.",
                  f"{base}#/wrapped/year")
        if now.month == 1 and now.day <= 7 and now.hour >= 10 and kv_get("season_sent") != f"{now.year - 1}-final" and not planned_done(f"gr-yearfinal-{now.year - 1}"):
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
        if cats.get("monthly", True) and today.day <= 3 and now.hour >= 9 and kv_get("monthly_sent") != mkey and not planned_done(f"gr-month-{mkey}"):
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
    # Wir zwei: Monatskrone
    today = dt.date.today()
    pa = kv_get("partner", None) if cfg.get("partner_url") and cfg.get("partner_code") else None
    if pa and cats.get("duo", True) and today.day <= 3 and dt.datetime.now().hour >= 9:
        prev = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        mkey = prev.strftime("%Y-%m")
        theirs = (pa.get("months") or {}).get(mkey)
        if theirs is not None and kv_get("crown_sent") != mkey:
            mine = monthly_minutes(3).get(mkey)
            if mine is not None and (mine or theirs):
                kv_set("crown_sent", mkey)
                me = (cfg.get("user_name") or "").strip() or "Du"
                names = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                         "Oktober", "November", "Dezember"]
                win = me if mine >= theirs else (pa.get("name") or "Partner")
                alert("duo", f"👑 {win} holt sich die {names[prev.month - 1]}-Krone",
                      f"{me} {mine / 60:.1f} Std · {pa.get('name') or 'Partner'} {theirs / 60:.1f} Std", "#/freunde")


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
    pd = os.path.join(WEB_DIR, "pets")
    if os.path.isdir(pd):
        for name in sorted(os.listdir(pd)):
            if name.endswith(".webp"):
                with open(os.path.join(pd, name), "rb") as f:
                    files["pets/" + name] = f.read()
    h = hashlib.sha256(b"".join(files[k] for k in sorted(files))).hexdigest()[:12]
    if "sw.js" in files:
        files["sw.js"] = files["sw.js"].replace(b"__APPHASH__", h.encode())
    return files, h


def recover_tiers(gh, cfg, max_versions=40):
    """Holt verlorene Tier-Einordnungen aus älteren Backups (GitHub-Verlauf von backup.enc) zurück.
    Füllt nur leere Einordnungen – nichts, was du danach selbst eingeordnet hast, wird überschrieben."""
    con = app_con()
    empty = {k for (k,) in con.execute("SELECT key FROM meta WHERE tier IS NULL")}
    con.close()
    if not empty:
        return {"restored": 0, "checked": 0}
    commits = gh.call("GET", f"/repos/{gh.repo}/commits?path=backup.enc&per_page={max_versions}&sha={gh.branch}") or []
    found, checked = {}, 0
    for c in commits:
        try:
            meta_ = gh.call("GET", f"/repos/{gh.repo}/contents/backup.enc?ref={c['sha']}")
            blob = gh.call("GET", f"/repos/{gh.repo}/git/blobs/{meta_['sha']}")
            b = cloud.decrypt(base64.b64decode(blob.get("content", "")), cfg["cloud_pass"])
        except Exception:
            continue
        checked += 1
        t = (b.get("tables") or {}).get("meta") or {}
        cols = t.get("cols") or []
        if "tier" not in cols or "key" not in cols:
            continue
        ki, ti = cols.index("key"), cols.index("tier")
        for r in t.get("rows") or []:
            k, tv = r[ki], r[ti]
            if k in empty and tv in TIERS and k not in found:   # neuester Stand zuerst
                found[k] = tv
        if len(found) >= len(empty):
            break
    con = app_con()
    with con:
        con.executemany("UPDATE meta SET tier = ? WHERE key = ? AND tier IS NULL", [(v, k) for k, v in found.items()])
    con.close()
    return {"restored": len(found), "checked": checked}


def build_snapshot(cfg, gh):
    cache = load_cache()
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
    try:
        pcs = pc_status_worker(cfg)
    except Exception as e:
        pcs = {"error": err_text(e)}
    return {
        "pc": pcs, "ago": year_ago(), "last_night": kv_get("last_night", None),
        "advent": kv_get("advent", {}) or {}, "wishes": kv_get("wishes", []) or [], "wish_done": kv_get("wish_done", {}) or {},
        "surprises": kv_get("surprises", []) or [], "capsule": kv_get("capsule", {}) or {}, "pins": kv_get("pins", []) or [],
        "features": kv_get("features", {}) or {}, "dates": kv_get("dates", {}) or {}, "fx": kv_get("fx", {}) or {},
        "seen_new": kv_get("seen_new", "") or "", "visits": kv_get("visits", []) or [], "pairs": kv_get("pairs", {}) or {}, "pet_names": {"acc": PET_ACC, "hab": PET_HAB}, "countdowns": countdowns(), "plans": kv_get("plans", []) or [], "plan_resp": kv_get("plan_resp", {}) or {},
        "meta": load_meta(), "hltb": hltb.load(APP_DB),
        "sessions": tracker.load_sessions(APP_DB, int(time.time()) - 800 * 86400),
        "csessions": load_console_sessions(int(time.time()) - 800 * 86400),
        "status_log": load_status_log(), "goals": kv_get("goals", {}), "goals_updated": kv_updated("goals"),
        "year_overrides": kv_get("year_overrides", {}), "wrapped_archive": kv_get("wrapped", {}), "notpc": kv_get("notpc", {}),
        "tracking_since": first_tracking_day(), "health": health(cfg), "new_games": new_games_seen(),
        "kv_updated": {k: kv_updated(k) for k in ("goals", "year_overrides", "wrapped", "notpc", "snotes", "covers", "paid", "notify", "critic")},
        "critic": kv_get("critic", {}),
        "notify": notify_prefs(cfg), "push_ready": bool((cfg.get("push_topic") or "").strip()),
        "covers": kv_get("covers", {}), "paid": kv_get("paid", {}),
        "partner": kv_get("partner", None) if (cfg.get("partner_url") and cfg.get("partner_code")) else None,
        "me_name": (cfg.get("user_name") or "").strip(),
        "me_duo": me_duo(cfg) if (cfg.get("partner_url") and cfg.get("partner_code")) else None,
        "greets": kv_get("greets", []) or [], "push_topic": (cfg.get("push_topic") or "").strip() or None, "partner_url": (cfg.get("partner_url") or "").strip() or None,
        "ui": kv_get("ui", {}) or {}, "ghist": game_history(), "pet": pet_state(cfg), "pet_raw": kv_get("pet", {}) or {}, "pet_imgs": sorted(n for n in os.listdir(os.path.join(WEB_DIR, "pets")) if n.endswith(".webp")) if os.path.isdir(os.path.join(WEB_DIR, "pets")) else [],
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
    # Alle anderen Tage nur mit dem, was sich geändert hat (für Begleiter, Wochen-Duell, Spielverlauf)
    changes, prev = [], {}
    for d in days:
        cur_ = {i: (p, m) for i, p, m in hc.execute("SELECT id, platform, minutes FROM snapshots WHERE day = ?", (d,))}
        if d not in keep:
            changes += [[d, i, p, m] for i, (p, m) in cur_.items() if prev.get(i, (None, None))[1] != m]
        prev = cur_
    hc.close()
    out["tables"]["snapshots"] = {"cols": ["day", "id", "platform", "minutes"], "rows": rows}
    out["snapshot_changes"] = changes
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
    ch = b.get("snapshot_changes") or []
    if ch:
        for r in ch:
            n += hc.execute("INSERT OR IGNORE INTO snapshots (day, id, platform, minutes) VALUES (?, ?, ?, ?)", r[:4]).rowcount
        # fehlende Spiele an diesen Tagen vom Vortag übernehmen (dort hat sich nichts geändert)
        days = [r[0] for r in hc.execute("SELECT DISTINCT day FROM snapshots ORDER BY day")]
        chdays = {r[0] for r in ch}
        prev = None
        for d in days:
            cur_ = dict((i, (p, m)) for i, p, m in hc.execute("SELECT id, platform, minutes FROM snapshots WHERE day = ?", (d,)))
            if prev is not None and d in chdays:
                for i, (p, m) in prev.items():
                    if i not in cur_:
                        hc.execute("INSERT OR IGNORE INTO snapshots (day, id, platform, minutes) VALUES (?, ?, ?, ?)", (d, i, p, m))
                        cur_[i] = (p, m)
            prev = cur_
    con.commit(); hc.commit(); con.close(); hc.close()
    return n


def cloud_notify_files(cfg):
    """Dateien für die Benachrichtigungen ohne PC (GitHub Action): Skript + Einstellungen (keine Geheimnisse)."""
    if not (cfg.get("push_topic") or "").strip():
        return {}, None
    prefs = notify_prefs(cfg)
    try:
        script = open(os.path.join(BASE, "cloudjob", "cloudnotify.py"), "rb").read()
    except OSError:
        return {}, None
    nj = {"cats": {k: bool((prefs.get("cats") or {}).get(k, True)) for k in ("free", "sales")}, "quiet": prefs.get("quiet"),
          "phone": bool(prefs.get("phone", True)), "tz": "Europe/Berlin", "enabled": True}
    files = {"cloudnotify.py": script, "notify.json": json.dumps(nj, indent=1).encode()}
    return files, hashlib.sha256(b"".join(files[k] for k in sorted(files))).hexdigest()[:12]


def cloud_notify_workflow(gh):
    """Die GitHub Action selbst anlegen. Braucht beim Token die Berechtigung „Workflows: Read and write“."""
    try:
        wf = open(os.path.join(BASE, "cloudjob", "gr-notify.yml"), "rb").read()
    except OSError:
        return
    h = hashlib.sha256(wf).hexdigest()[:12]
    if cloud_state.get("cn_wf") == h:
        return
    try:
        gh.commit({".github/workflows/gr-notify.yml": wf}, message="Gaming Recap: Benachrichtigungen ohne PC")
        cloud_state["cn_wf"], cloud_state["cn_wf_error"] = h, None
    except Exception as e:
        cloud_state["cn_wf_error"] = err_text(e)


def cloud_notify_check(gh):
    """Läuft die Action? (liest ihren Stand aus dem Repository, höchstens stündlich)"""
    if time.time() - cloud_state.get("cn_checked", 0) < (3600 if cloud_notify_active() else 600):   # noch nicht aktiv: alle 10 Min. nachsehen
        return
    cloud_state["cn_checked"] = time.time()
    try:
        st = json.loads(gh.read_file({"path": "notify-state.json"}))
        kv_set("cloud_notify", {"last_day": st.get("last_day"), "status": st.get("status") or [], "checked": now_iso()})
    except FileNotFoundError:
        kv_set("cloud_notify", {"last_day": None, "status": ["noch nicht gelaufen"], "checked": now_iso()})
    except Exception:
        pass


def cloud_notify_active():
    """Übernimmt die Action Gratisspiele und Angebote? Dann schickt der PC sie nicht zusätzlich aufs Handy."""
    c = kv_get("cloud_notify", None) or {}
    if not c.get("last_day") or any("NTFY_TOPIC" in x for x in c.get("status") or []):
        return False
    return (dt.date.today() - dt.date.fromisoformat(c["last_day"])).days <= 1


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
        cloud_state["urgent"] = False
        if applied:
            try:
                plan_push_out(cfg)   # Zockabend-Antworten vom Handy gleich weitergeben
            except Exception as e:
                print("  Zockabend:", err_text(e))
        if not kv_get("tiers_recovered_v1"):
            try:
                r = recover_tiers(gh, cfg)
                kv_set("tiers_recovered_v1", dict(r, at=now_iso()))
                print(f"  Tier-Liste: {r['restored']} Einordnungen aus {r['checked']} Backups zurückgeholt")
            except Exception as e:
                print("  Tier-Liste zurückholen:", err_text(e))
        snap = build_snapshot(cfg, gh)
        digest = hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()
        files = {}
        if digest != cloud_state.get("data_hash") or force:
            snap["pushed"] = now_iso()
            files["data.enc"] = cloud.encrypt(snap, cfg["cloud_pass"])
            files["stamp.json"] = json.dumps({"pushed": snap["pushed"]}).encode()   # winzig: das Handy schaut hier, ob es Neues gibt
        today = dt.date.today().isoformat()
        if cloud_state.get("backup_day") != today:
            files["backup.enc"] = cloud.encrypt(build_backup(), cfg["cloud_pass"])
        try:
            cloud_notify_workflow(gh)
            cloud_notify_check(gh)
        except Exception as e:
            print("  Benachrichtigungen ohne PC:", err_text(e))
        app, app_hash = app_files()
        if app_hash != cloud_state.get("app_hash"):
            files.update(app)
        cn, cn_hash = cloud_notify_files(cfg)
        if cn and cn_hash != cloud_state.get("cn_hash"):
            files.update(cn)
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
                pdata = cloud.encrypt(partner_summary(cfg), cfg["partner_code"].strip())
                # Code mit in den Hash: nach einem Code-Wechsel muss neu verschlüsselt hochgeladen werden
                ph = hashlib.sha256((cfg["partner_code"].strip() + "|" + json.dumps(partner_summary(cfg, stable=True), sort_keys=True, default=str)).encode()).hexdigest()
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
        if cn and "notify.json" in files:
            cloud_state["cn_hash"] = cn_hash
        cloud_state.update(last_push=now_iso(), error=None, data_hash=digest, app_hash=app_hash,
                           url=gh.pages_url(), pending=False, applied=applied)
    except Exception as e:
        cloud_state.update(error=err_text(e))
        print("  Cloud:", cloud_state["error"])
    finally:
        cloud_state["last_try"] = time.time()
        write_json(CLOUD_STATE_PATH, cloud_state)
        cloud_lock.release()


def psn_changed(cfg):
    """Leichter PS5-Check: hat sich Spielzeit oder „zuletzt gespielt“ seit dem letzten Abgleich geändert?"""
    try:
        now = {g["id"]: (g["minutes"], g.get("last_played")) for g in fetch_psn(cfg, [])}
    except Exception:
        return False
    old = {g["id"]: (g["minutes"], g.get("last_played")) for g in load_cache().get("games", []) if str(g.get("id", "")).startswith("psn_")}
    return any(old.get(k) != v for k, v in now.items())


def scheduler():
    last_export, last_run, last_cloud, last_edits, last_health, started = None, 0, 0, 0, 0, time.time()
    last_psn = time.time()
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
            last_psn = time.time()
        elif (cfg.get("psn_npsso") or "").strip() and time.time() - last_psn > 600 and time.time() - last_run > 600:
            # PS5 alle 10 Min. kurz fragen; nur wenn sich etwas getan hat, voll abgleichen
            last_psn = time.time()
            if psn_changed(cfg):
                last_run = time.time()
                run_sync()
        # Cloud: Änderungen sofort (max. alle 5 Min.), sonst alle 15 Min. nach Handy-Änderungen schauen
        try:
            check_notifications(cfg)
            try:
                handle_remote(cfg)
            except Exception as e:
                print("  Fernbedienung:", e)
            if time.time() - state.get("planned_t", 0) > 3600 and time.time() - started > 120:
                state["planned_t"] = time.time()
                threading.Thread(target=plan_ahead, args=(cfg,), daemon=True).start()
            if time.time() - last_health > 600 and time.time() - started > 300:
                last_health = time.time()
                check_health(cfg)
                check_expiry(cfg)
                try:
                    check_pet(cfg)
                except Exception as e:
                    print("  Begleiter:", err_text(e))
            offline_watchdog(cfg)
            if time.time() - started > (45 if just_updated() else 300):   # nach einem Update gleich nachsehen, ob noch etwas fehlt
                threading.Thread(target=update_worker, args=(cfg,), daemon=True).start()
            cur_play = tuple(sorted(str(p.get("key")) for p in TRACKER.current or []))
            if cfg.get("partner_code") and cur_play != partner_state.get("play"):
                partner_state["play"] = cur_play
                cloud_state["pending"] = True  # „spielt gerade“ zügig an den Partner geben
            try:
                plan_push_out(cfg)
            except Exception as e:
                print("  Zockabend:", err_text(e))
            try:   # Downloads & Speicher: läuft ein Download, alle ~30 Sek. frisch – Änderungen gehen gleich ans Handy
                pv = pc_status_worker(cfg)
                sig = tuple((d["name"], d["pct"] // 10) for d in pv.get("downloads") or [])
                if sig != pcstat.get("sig"):
                    if "sig" in pcstat:
                        cloud_state["pending"] = True
                    pcstat["sig"] = sig
            except Exception:
                pass
            if cfg.get("partner_url") and time.time() - partner_state.get("greet_t", 0) > 25:
                partner_state["greet_t"] = time.time()
                threading.Thread(target=poll_greets, args=(cfg,), daemon=True).start()
            if cfg.get("partner_url") and time.time() - partner_state["last"] > partner_every():
                threading.Thread(target=fetch_partner, args=(cfg,), daemon=True).start()
        except Exception as e:
            print("  Hinweise:", e)
        if cloud_ready(cfg) and not state["syncing"]:
            gap = time.time() - last_cloud
            has_edits = False
            if time.time() - last_edits > 30:  # Handy-Änderungen: alle 30 Sek. nachsehen (1 kleine Anfrage)
                last_edits = time.time()
                try:
                    has_edits = bool(cloud.GitHub(cfg["gh_repo"], cfg["gh_token"]).list_edits())
                except Exception:
                    pass
            if has_edits or (cloud_state.get("pending") and (gap > 60 or cloud_state.get("urgent"))) or gap > 900:
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
        if re.fullmatch(r"/pets/[a-z][a-z0-9_]{1,40}\.webp", path):
            fp = os.path.join(WEB_DIR, "pets", path.rsplit("/", 1)[1])
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "image/webp", cache=True)
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
        if path == "/api/backup/local":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            return self._send(200, {"list": list_backups()})
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
            cfg.update(autostart=bool(af and (os.path.exists(af) or other_autostart())), autostart_supported=bool(af))
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
        if path == "/api/remote":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            if body.get("cmd") not in REMOTE_CMDS:
                return self._send(400, {"error": "Unbekannter Befehl"})
            try:
                remote_exec(load_config(), body["cmd"], body.get("arg"))
                return self._send(200, {"ok": True, "pc": pc_status_worker(load_config(), force=True)})
            except Exception as e:
                return self._send(500, {"error": err_text(e)})
        if path == "/api/selfcheck":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            return self._send(200, {"checks": self_check()})
        if path == "/api/tiers/recover":
            if not self._local():
                return self._send(403, {"error": "Nur am PC"})
            cfg = load_config()
            if not cloud_ready(cfg):
                return self._send(400, {"error": "Handy-Cloud ist nicht eingerichtet."})
            try:
                return self._send(200, recover_tiers(cloud.GitHub(cfg["gh_repo"], cfg["gh_token"]), cfg))
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
        if path == "/api/backup/local":
            try:
                return self._send(200, {"name": local_backup("manuell")})
            except Exception as e:
                return self._send(500, {"error": err_text(e)})
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
            val = merge_pet(body.get("value")) if body["key"] == "pet" else clean_kv(body["key"], body.get("value"))
            if body["key"] == "wrapped":  # abgeschlossene Zeiträume bleiben, wie sie sind
                old = kv_get("wrapped", {}) or {}
                for k, a in old.items():
                    if a.get("final"):
                        val[k] = a
            kv_set(body["key"], val)
            cloud_state["pending"] = True
            if body["key"] in PLAN_KEYS:
                cloud_state["urgent"] = True
                threading.Thread(target=plan_push_out, args=(load_config(),), daemon=True).start()
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
                      "notify_free", "notify_release", "publish_updates", "auto_update", "prices_steam_only", "allow_wake"):
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
    if "--restore" in sys.argv:
        i = sys.argv.index("--restore")
        restore_local(sys.argv[i + 1] if len(sys.argv) > i + 1 else "latest")
        return
    if "--backup" in sys.argv:
        print("Gesichert:", local_backup("manuell"))
        return
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
    windows_housekeeping()
    try:
        pet_soft_reset()
    except Exception as e:
        print("  Begleiter-Neustart:", e)
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
