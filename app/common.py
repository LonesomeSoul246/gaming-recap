"""Gemeinsame Helfer für Gaming Recap."""
import datetime as dt
import json
import os
import re
import unicodedata
import urllib.request
import shutil
import socket
import subprocess
import time
import uuid

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# Plattform-Zusätze, die dasselbe Spiel auf verschiedenen Systemen unterscheiden.
# Muss identisch zu PLATFORM_SUFFIX in web/index.html bleiben.
PLATFORM_SUFFIX = re.compile(
    r"\((?:playstation ?[45]|ps[45]|xbox one|xbox series x ?\| ?s|windows|pc)\)"
    r"|\b(?:for )?xbox series x ?\| ?s\b|\bxbox one edition\b|-\s*windows(?: 10)?(?: edition)?\b"
    r"|\bpc edition\b|\bfor windows 10\b|\bstandard edition\b")

# Apps, die keine Spiele sind (exakter Vergleich mit dem normalisierten Titel)
NON_GAMES = {
    "youtube", "youtube tv", "youtube kids", "netflix", "spotify", "twitch", "prime video", "amazon prime video",
    "disney", "disney plus", "wow", "media player", "apple tv", "dazn", "crunchyroll", "joyn", "rtl", "rtl plus",
    "plex", "paramount", "magenta tv", "zdfmediathek", "ard mediathek", "sky go", "sky ticket", "waipu tv", "hulu",
    "max", "hbo max", "vlc", "kodi", "web browser", "internet browser", "playstation vue", "tv video", "ps vr",
    "live from playstation", "playstation video", "playstation music", "funimation", "pluto tv", "rakuten tv",
    "microsoft edge", "movies tv", "filme tv", "xbox app", "xbox", "game pass", "eurosport", "dazn player",
    # PC-Werkzeuge aus Steam, die laufen, ohne dass gespielt wird
    "wallpaper engine", "soundpad", "steamvr", "lossless scaling", "obs studio", "fpsvr", "ovr advanced settings",
    "ovr toolkit", "virtual desktop", "voicemeeter", "steam link", "steamworks common redistributables",
}


# Editions-Zusätze am Ende, damit z. B. "GTA V Enhanced" und "GTA V" ein Spiel sind
EDITION_SUFFIX = re.compile(
    r" (?:enhanced(?: edition)?|definitive edition|game of the year(?: edition)?|goty(?: edition)?|remastered"
    r"|complete edition|deluxe edition|ultimate edition|gold edition|special edition|anniversary edition"
    r"|director s cut|(?:the )?final cut)$")


def norm_title(t):
    t = re.sub(r"[™®©℠]", " ", str(t or ""))  # vor NFKD entfernen, sonst wird ™ zu "TM"
    t = re.sub(r"[‘’‛′´`]", "'", t)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = PLATFORM_SUFFIX.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return EDITION_SUFFIX.sub("", t).strip() or t


APP_CATEGORIES = ("media", "videoservice", "web_based", "nongame", "_app")


def is_non_game(title, category=None):
    """Nur eindeutige App-Kategorien (z. B. ps5_web_based_media_app) oder bekannte App-Namen."""
    cat = str(category or "").lower()
    if cat and any(k in cat for k in APP_CATEGORIES):
        return True
    return norm_title(title) in NON_GAMES


def parse_iso(s):
    """ISO-Zeit (auch mit 7 Nachkommastellen aus PowerShell) -> Unix-Sekunden."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return int(d.timestamp())  # naive Zeit = lokale Zeit


def http_json(url, data=None, headers=None, method=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ===== PC-Helfer: Steam-Downloads und Spielgrößen, Speicherplatz, Herunterfahren/Ruhezustand, Wake-on-LAN =====
# (liegt hier statt in einer eigenen Datei, damit ältere Updater die neue Fassung ohne Umweg annehmen)
def steam_libraries():
    if os.name != "nt":
        return []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        return []
    libs = {os.path.normpath(steam)}
    try:
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="ignore") as f:
            for p in re.findall(r'"path"\s+"([^"]+)"', f.read()):
                libs.add(os.path.normpath(p.replace("\\\\", "\\")))
    except OSError:
        pass
    return sorted(libs)


def _acf(text, key):
    m = re.search(r'"%s"\s+"([^"]*)"' % key, text, re.I)
    return m.group(1) if m else None


def parse_manifest(text):
    """Steam-appmanifest → {appid, name, size, state, done, total, installdir}."""
    def num(k):
        v = _acf(text, k)
        return int(v) if v and v.isdigit() else 0
    flags = num("StateFlags")
    total, done = num("BytesToDownload"), num("BytesDownloaded")
    # 4 = fertig installiert; 1024/1026/1042 = Update/Download läuft; 512 = Update geplant
    downloading = bool(flags & 1024) or (total and done < total and not flags & 4)
    return {"appid": _acf(text, "appid"), "name": _acf(text, "name"), "size": num("SizeOnDisk"), "state": flags,
            "downloading": bool(downloading), "done": done, "total": total, "installdir": _acf(text, "installdir"),
            "last_played": num("LastPlayed") or None}


def steam_apps(libs=None):
    out = []
    for lib in (libs if libs is not None else steam_libraries()):
        sa = os.path.join(lib, "steamapps")
        try:
            names = os.listdir(sa)
        except OSError:
            continue
        for n in names:
            if re.fullmatch(r"appmanifest_\d+\.acf", n):
                try:
                    with open(os.path.join(sa, n), encoding="utf-8", errors="ignore") as f:
                        m = parse_manifest(f.read())
                except OSError:
                    continue
                m["lib"] = lib
                m["path"] = os.path.join(sa, "common", m["installdir"] or "")
                out.append(m)
    return out


def folder_size(path, limit_s=20):
    """Größe eines Ordners in Bytes (bricht nach limit_s Sekunden ab → None)."""
    t0, tot = time.time(), 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                tot += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
        if time.time() - t0 > limit_s:
            return None
    return tot


def drives(paths):
    """Freier Platz der Laufwerke, auf denen die Pfade liegen: {Laufwerk: (frei, gesamt)}."""
    out = {}
    for p in paths:
        if not p:
            continue
        root = os.path.splitdrive(os.path.abspath(p))[0] + os.sep if os.name == "nt" else "/"
        if root in out:
            continue
        try:
            u = shutil.disk_usage(root)
            out[root] = (u.free, u.total)
        except OSError:
            pass
    return out


def mac_address():
    n = uuid.getnode()
    if (n >> 40) & 1:        # zufällige Adresse (keine echte Netzwerkkarte gefunden)
        return None
    return ":".join(f"{(n >> s) & 0xff:02x}" for s in range(40, -1, -8))


def wake(mac, broadcast="255.255.255.255"):
    """Wake-on-LAN: magisches Paket an die Netzwerkkarte schicken."""
    m = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(m) != 12:
        raise ValueError("Ungültige MAC-Adresse")
    pkt = b"\xff" * 6 + bytes.fromhex(m) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(pkt, (broadcast, port))
    return True


def power(action, delay=60, msg="Gaming Recap: Gute Nacht!"):
    """shutdown (mit Vorwarnung), sleep, abort. Nur unter Windows."""
    if os.name != "nt":
        raise RuntimeError("Nur unter Windows")
    flags = 0x08000000   # kein Fenster
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", str(int(delay)), "/c", msg], creationflags=flags)
    elif action == "abort":
        subprocess.Popen(["shutdown", "/a"], creationflags=flags)
    elif action == "sleep":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=flags)
    else:
        raise ValueError(action)
    return True
