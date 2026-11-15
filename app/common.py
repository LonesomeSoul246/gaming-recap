"""Gemeinsame Helfer für Gaming Recap."""
import datetime as dt
import json
import os
import re
import unicodedata
import urllib.request

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
