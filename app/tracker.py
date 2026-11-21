"""
Session-Tracker: erkennt laufende PC-Spiele anhand ihrer Installationsordner
und speichert jede Spielsession minutengenau in daten/app.db.
"""
import json
import os
import re
import sqlite3
import threading
import time

from common import norm_title, parse_iso

# Hilfsprogramme, die nicht als "Spiel läuft" zählen
IGNORE = re.compile(r"(crash|report|unins|setup|redist|vcredist|dxsetup|prereq|webhelper|helper|easyanticheat|eac_|"
                    r"battleye|beservice|cefprocess|overlay|updater|bootstrap|install|launcherpatcher|uplaywebcore)", re.I)


def init_db(con):
    con.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, game_key TEXT, title TEXT, "
                "start INTEGER, end INTEGER, seconds INTEGER, source TEXT)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS sessions_u ON sessions (game_key, start, source)")
    con.execute("CREATE INDEX IF NOT EXISTS sessions_start ON sessions (start)")
    con.execute("CREATE TABLE IF NOT EXISTS afk (day TEXT, game_key TEXT, seconds INTEGER, PRIMARY KEY (day, game_key))")


# ---------------------------------------------------------------- AFK: keine Eingabe (Maus, Tastatur, Controller)

_xin = {"dll": None, "pk": {}, "t": 0}


def idle_seconds():
    """Sekunden seit der letzten Eingabe. Maus/Tastatur über Windows, Xbox-/Steam-Controller über XInput.
    Andere Systeme: 0 (= nie AFK)."""
    if os.name != "nt":
        return 0
    import ctypes
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LII(ctypes.sizeof(LII), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0
    idle = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
    try:   # Controller: Paketnummer ändert sich bei jeder Eingabe
        if _xin["dll"] is None:
            for n in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
                try:
                    _xin["dll"] = getattr(ctypes.windll, n)
                    break
                except OSError:
                    continue
        if _xin["dll"]:
            class XS(ctypes.Structure):
                _fields_ = [("pk", ctypes.c_uint), ("b", ctypes.c_ushort), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                            ("lx", ctypes.c_short), ("ly", ctypes.c_short), ("rx", ctypes.c_short), ("ry", ctypes.c_short)]
            now = time.time()
            for i in range(4):
                st = XS()
                if _xin["dll"].XInputGetState(i, ctypes.byref(st)) == 0:
                    if _xin["pk"].get(i) not in (None, st.pk):
                        _xin["t"] = now
                    _xin["pk"][i] = st.pk
            if _xin["t"]:
                idle = min(idle, now - _xin["t"])
    except Exception:
        pass
    return idle


# ---------------------------------------------------------------- Windows-Prozesse

PROC_START = {}  # Pfad der .exe -> Startzeit (Unix) des ältesten laufenden Prozesses

def _running_exes_windows():
    import ctypes
    from ctypes import wintypes as wt
    psapi = ctypes.WinDLL("psapi")
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    k32.GetProcessTimes.restype = wt.BOOL
    starts = {}
    arr = (wt.DWORD * 8192)()
    needed = wt.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
        return set()
    out = set()
    buf = ctypes.create_unicode_buffer(1024)
    for pid in arr[: needed.value // ctypes.sizeof(wt.DWORD)]:
        if not pid:
            continue
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            size = wt.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                exe = buf.value
                out.add(exe)
                ft = [wt.FILETIME() for _ in range(4)]
                if k32.GetProcessTimes(h, *[ctypes.byref(x) for x in ft]):
                    t = ((ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime) / 1e7 - 11644473600
                    if t > 0 and (exe not in starts or t < starts[exe]):
                        starts[exe] = int(t)
        finally:
            k32.CloseHandle(h)
    PROC_START.clear()
    PROC_START.update(starts)
    return out


def running_exes():
    if os.name != "nt":
        return set()
    try:
        return _running_exes_windows()
    except Exception:
        return set()


def steam_install_dirs():
    """Steam-AppID -> Installationsordner (aus den Steam-Manifesten)."""
    if os.name != "nt":
        return {}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        return {}
    libs = {os.path.normpath(steam)}
    try:
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="ignore") as f:
            for p in re.findall(r'"path"\s+"([^"]+)"', f.read()):
                libs.add(os.path.normpath(p.replace("\\\\", "\\")))
    except OSError:
        pass
    out = {}
    for lib in libs:
        sa = os.path.join(lib, "steamapps")
        try:
            names = os.listdir(sa)
        except OSError:
            continue
        for n in names:
            m = re.fullmatch(r"appmanifest_(\d+)\.acf", n)
            if not m:
                continue
            try:
                with open(os.path.join(sa, n), encoding="utf-8", errors="ignore") as f:
                    d = re.search(r'"installdir"\s+"([^"]+)"', f.read())
            except OSError:
                continue
            if d:
                out[m.group(1)] = os.path.join(sa, "common", d.group(1))
    return out


# ---------------------------------------------------------------- Tracker

class Tracker:
    GAP = 180          # Sekunden ohne Lebenszeichen -> Session beendet
    RESUME = 300       # Neustart innerhalb von 5 Min. setzt die Session fort
    MIN_SECONDS = 60   # kürzere Sessions werden verworfen (Launcher-Updates, Abstürze)
    AFK_AFTER = 1800   # 30 Min ohne Eingabe bei laufendem Spiel = AFK (zählt nicht für Begleiter und Bosse)

    def __init__(self, db_path, exes_fn=running_exes, idle_fn=idle_seconds):
        self.idle_fn = idle_fn
        self.afk_mark = (0, 0)   # (Zeitpunkt der letzten Eingabe, davon schon als AFK gezählt)
        self.db_path = db_path
        self.exes_fn = exes_fn
        self.targets = []
        self.open = {}
        self.lock = threading.Lock()
        self.current = []
        self.last_ok = 0
        self.started = {}

    def _con(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        init_db(con)
        return con

    def set_targets(self, items):
        """items: Liste (ordner, key, titel)."""
        t = []
        for d, key, title in items:
            if not d:
                continue
            nd = os.path.normcase(os.path.normpath(d))
            if nd.rstrip(os.sep).count(os.sep) < 2:  # z. B. "C:\Spiele" ist zu allgemein
                continue
            if os.path.basename(nd) in ("common", "steamapps", "games", "spiele", "program files", "program files (x86)"):
                continue
            t.append((nd.rstrip(os.sep) + os.sep, key, title))
        t.sort(key=lambda x: -len(x[0]))
        with self.lock:
            self.targets = t

    def match(self, exes):
        with self.lock:
            targets = list(self.targets)
        found = {}
        self.started = {}
        for exe in exes:
            if IGNORE.search(os.path.basename(exe)):
                continue
            ne = os.path.normcase(exe)
            for d, key, title in targets:
                if ne.startswith(d):
                    found[key] = title
                    st = PROC_START.get(exe)
                    if st and (key not in self.started or st < self.started[key]):
                        self.started[key] = st
                    break
        return found

    def step(self, now=None):
        now = int(now or time.time())
        found = self.match(self.exes_fn())
        con = self._con()
        with con:
            for key, title in found.items():
                o = self.open.get(key)
                if not (o and now - o["last"] <= self.GAP):
                    row = con.execute("SELECT id, start FROM sessions WHERE game_key=? AND source='tracker' AND end>=? "
                                      "ORDER BY end DESC LIMIT 1", (key, now - self.RESUME)).fetchone()
                    if row:
                        o = {"id": row[0], "start": row[1]}
                    else:
                        # Lief das Spiel schon, bevor Gaming Recap (neu) gestartet ist? Dann ab Prozessstart zählen.
                        start = now
                        st = (getattr(self, "started", {}) or {}).get(key)
                        if st and now - 12 * 3600 < st < now - 30:
                            last = con.execute("SELECT MAX(end) FROM sessions WHERE game_key=?", (key,)).fetchone()[0] or 0
                            start = max(st, last + 1)
                        cur = con.execute("INSERT OR IGNORE INTO sessions (game_key, title, start, end, seconds, source) "
                                          "VALUES (?, ?, ?, ?, ?, 'tracker')", (key, title, start, now, now - start))
                        o = {"id": cur.lastrowid, "start": start}
                    self.open[key] = o
                o["last"] = now
                con.execute("UPDATE sessions SET end=?, seconds=? - start WHERE id=?", (now, now, o["id"]))
            for key in list(self.open):
                if key not in found:
                    del self.open[key]
            try:
                idle = int(self.idle_fn() or 0)
            except Exception:
                idle = 0
            if found and idle >= self.AFK_AFTER:
                last_input = now - idle
                mark, counted = self.afk_mark if abs(self.afk_mark[0] - last_input) < 30 else (last_input, 0)
                add = idle - counted
                if add > 0:
                    day = time.strftime("%Y-%m-%d", time.localtime(now))
                    for key in found:
                        con.execute("INSERT INTO afk VALUES (?, ?, ?) ON CONFLICT(day, game_key) DO UPDATE SET seconds = seconds + ?",
                                    (day, key, add, add))
                self.afk_mark = (mark, idle)
            con.execute("DELETE FROM sessions WHERE source='tracker' AND seconds < ? AND end < ?",
                        (self.MIN_SECONDS, now - self.RESUME - 60))
        con.close()
        self.current = [{"key": k, "title": found[k], "since": self.open[k]["start"]} for k in found]
        self.last_ok = now
        return found

    def afk_minutes(self, since_day):
        """{tag: {spielschlüssel: Minuten}}"""
        con = self._con()
        rows = con.execute("SELECT day, game_key, seconds FROM afk WHERE day >= ?", (since_day,)).fetchall()
        con.close()
        out = {}
        for d, k, sec in rows:
            out.setdefault(d, {})[k] = sec // 60
        return out

    def run(self, tick=20):
        while True:
            try:
                self.step()
            except Exception as e:
                print("Tracker-Fehler:", e)
            time.sleep(tick)


def import_playnite_sessions(path, db_path, idmap=None):
    """Liest sessions.jsonl der Playnite-Erweiterung. Überspringt Sessions, die der Tracker schon hat."""
    if not path or not os.path.exists(path):
        return 0
    con = sqlite3.connect(db_path, timeout=10)
    init_db(con)
    n = 0
    with con, open(path, encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            start, end = parse_iso(j.get("start")), parse_iso(j.get("end"))
            if not start or not end:
                continue
            sec = int(j.get("seconds") or end - start)
            if sec < Tracker.MIN_SECONDS:
                continue
            key = (idmap or {}).get(j.get("id")) or norm_title(j.get("name"))
            if con.execute("SELECT 1 FROM sessions WHERE game_key=? AND source='tracker' AND start<? AND end>?",
                           (key, end, start)).fetchone():
                continue
            cur = con.execute("INSERT OR IGNORE INTO sessions (game_key, title, start, end, seconds, source) "
                              "VALUES (?, ?, ?, ?, ?, 'playnite')", (key, j.get("name"), start, end, sec))
            n += cur.rowcount
    con.close()
    return n


def load_sessions(db_path, since):
    con = sqlite3.connect(db_path, timeout=10)
    init_db(con)
    rows = con.execute("SELECT game_key, start, seconds FROM sessions WHERE start>=? AND seconds>=? ORDER BY start",
                       (since, Tracker.MIN_SECONDS)).fetchall()
    con.close()
    return [[k, s, sec] for k, s, sec in rows]
