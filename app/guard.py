"""
Wächter nach einem Update: startet Gaming Recap und prüft, ob es antwortet.
Antwortet es nach 60 Sekunden nicht, wird die vorige Fassung wiederhergestellt und gestartet.
Nur Python-Standardbibliothek; wird von server.py nach einem Update aufgerufen.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def data_dir():
    local = os.path.join(BASE, "daten")
    if os.path.isdir(local):
        return local
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "GamingRecap", "daten")


def log(msg):
    try:
        with open(os.path.join(data_dir(), "protokoll.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M ") + "[Update] " + msg + "\n")
    except OSError:
        pass


def start():
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.name == "nt" and os.path.exists(w):
        exe = w
    kw = {"cwd": BASE, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    return subprocess.Popen([exe, os.path.join(BASE, "server.py"), "--no-browser", "--restarted"], **kw)


def alive(seconds=60):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/api/config", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main():
    time.sleep(1.5)  # alter Prozess beendet sich
    p = start()
    if alive():
        log("neue Fassung läuft")
        return
    log("neue Fassung startet nicht – stelle die vorige wieder her")
    try:
        p.kill()
    except Exception:
        pass
    try:
        backup = os.path.join(data_dir(), "app_sicherung")
        with open(os.path.join(backup, "dateien.json"), encoding="utf-8") as f:
            info = json.load(f)
        import shutil
        for rel in info.get("files", []):
            src = os.path.join(backup, *rel.split("/"))
            if os.path.isfile(src):
                dst = os.path.join(BASE, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
        for rel in info.get("new", []):
            if rel not in info.get("files", []):
                try:
                    os.remove(os.path.join(BASE, *rel.split("/")))
                except OSError:
                    pass
        with open(os.path.join(data_dir(), "update_gesperrt.json"), "w", encoding="utf-8") as f:
            json.dump({"release": info.get("release"), "at": int(time.time())}, f)
        start()
        log("vorige Fassung wiederhergestellt – dieses Update wird übersprungen")
    except Exception as e:
        log(f"Wiederherstellen fehlgeschlagen: {e}")


if __name__ == "__main__":
    main()
