"""
Updates für weitergegebene Installationen.

Veröffentlichen (Marvins PC): Die Programmdateien landen beim Cloud-Abgleich unter app/ im eigenen
GitHub-Pages-Repository, dazu app/version.json mit SHA-256 jeder Datei. Keine Keys, keine Daten.

Empfangen (andere PCs): Einmal am Tag version.json lesen, geänderte Dateien laden und prüfen,
alte Fassung sichern, austauschen und über guard.py neu starten. Startet die neue Fassung nicht,
spielt guard.py die gesicherte Fassung zurück.
"""
import hashlib
import re
import json
import os
import shutil
import time
import urllib.parse
import urllib.request

PROGRAM_FILES = [
    "server.py", "cloud.py", "common.py", "extras.py", "hltb.py", "media.py", "notify.py", "steamx.py",
    "tracker.py", "updater.py", "guard.py", "Start.bat", "Start mit Fenster.bat", "ANLEITUNG.txt", "Zuruecksetzen.bat",
    "web/index.html", "web/sw.js", "web/manifest.webmanifest", "web/icon-192.png", "web/icon-512.png",
    "web/apple-touch-icon.png", "cloudjob/cloudnotify.py", "cloudjob/gr-notify.yml",
] + [f"web/pets/{n}_{st}.webp" for n in ("brasu", "neruma", "moselle", "crysto", "nocti", "koppla") for st in ("ei", "1", "2", "3")] + [
    "playnite-erweiterung/GamingRecapExport/extension.yaml",
    "playnite-erweiterung/GamingRecapExport/GamingRecap.psm1",
]


_SAFE_TOP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,60}\.(py|bat|txt)$")
_SAFE_SUB = re.compile(r"^(web|cloudjob|playnite-erweiterung)/[A-Za-z0-9 _./-]{1,160}$")


def allowed(rel):
    """Welche Pfade ein Update anfassen darf: bekannte Programmdateien und neue Programmdateien im Hauptordner
    (.py/.bat/.txt) oder in web/, cloudjob/, playnite-erweiterung/. Nie Daten, Konfiguration oder Pfade nach außen."""
    if not isinstance(rel, str) or ".." in rel or "\\" in rel or rel.startswith("/") or ":" in rel:
        return False
    return rel in PROGRAM_FILES or bool(_SAFE_TOP.match(rel)) or bool(_SAFE_SUB.match(rel))


def sha(b):
    return hashlib.sha256(b).hexdigest()


def program_files(base):
    out = {}
    pd = os.path.join(base, "web", "pets")   # alle Begleiter-/Boss-/Accessoire-Bilder, auch später hinzugekommene
    extra = [f"web/pets/{n}" for n in sorted(os.listdir(pd)) if n.endswith(".webp")] if os.path.isdir(pd) else []
    for rel in list(dict.fromkeys(PROGRAM_FILES + extra)):
        p = os.path.join(base, *rel.split("/"))
        if os.path.isfile(p):
            with open(p, "rb") as f:
                out[rel] = f.read()
    return out


# Diese Pfade kennt jeder bisher ausgelieferte Updater. Alles andere steht in „extra“, das ältere
# Updater überspringen – sonst lehnen sie die ganze Liste ab („Update-Liste ungültig“). Nach dem ersten
# Update haben sie diese Fassung und holen „extra“ beim nächsten Prüfen nach.
LEGACY_TOP = ("server.py", "cloud.py", "common.py", "extras.py", "hltb.py", "media.py", "notify.py", "steamx.py",
              "tracker.py", "updater.py", "guard.py", "Start.bat", "Start mit Fenster.bat", "ANLEITUNG.txt")


def legacy_ok(rel):
    return rel in LEGACY_TOP or rel.startswith(("web/", "playnite-erweiterung/"))


def manifest(files, version):
    hashes = {rel: sha(b) for rel, b in files.items()}
    release = sha("".join(f"{k}:{v}" for k, v in sorted(hashes.items())).encode())[:10]
    return {"version": version, "release": release, "at": int(time.time()),
            "files": {k: v for k, v in hashes.items() if legacy_ok(k)},
            "extra": {k: v for k, v in hashes.items() if not legacy_ok(k)}}


def all_files(man):
    return {**(man.get("files") or {}), **(man.get("extra") or {})}


def local_release(base, version):
    return manifest(program_files(base), version)["release"]


def _get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "GamingRecap-Updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check(update_url, base, version):
    """Liefert (manifest, geänderte Dateien) – leere Liste, wenn alles aktuell ist."""
    url = update_url.rstrip("/") + "/version.json?t=" + str(int(time.time()))
    man = json.loads(_get(url).decode("utf-8"))
    files = all_files(man)
    if not man.get("files") or any(not allowed(rel) for rel in files):
        raise ValueError("Update-Liste ungültig")
    local = program_files(base)
    changed = [rel for rel, h in files.items() if sha(local.get(rel, b"")) != h]
    return man, changed


def apply(update_url, base, data_dir, man, changed):
    """Lädt, prüft und tauscht die Dateien. Gibt den Sicherungsordner zurück."""
    staged = {}
    for rel in changed:
        url = update_url.rstrip("/") + "/" + urllib.parse.quote(rel) + "?r=" + man["release"]
        b = _get(url)
        if sha(b) != all_files(man)[rel]:
            raise ValueError(f"Prüfsumme passt nicht: {rel}")
        staged[rel] = b
    backup = os.path.join(data_dir, "app_sicherung")
    if os.path.isdir(backup):
        shutil.rmtree(backup, ignore_errors=True)
    os.makedirs(backup, exist_ok=True)
    old = program_files(base)
    for rel, b in old.items():
        p = os.path.join(backup, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b)
    with open(os.path.join(backup, "dateien.json"), "w", encoding="utf-8") as f:
        json.dump({"files": list(old), "new": list(staged), "release": man["release"], "at": int(time.time())}, f)
    for rel, b in staged.items():
        p = os.path.join(base, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".neu"
        with open(tmp, "wb") as f:
            f.write(b)
        os.replace(tmp, p)
    return backup


def rollback(base, data_dir):
    """Stellt die gesicherte Fassung wieder her (von guard.py genutzt)."""
    backup = os.path.join(data_dir, "app_sicherung")
    info_p = os.path.join(backup, "dateien.json")
    if not os.path.exists(info_p):
        return False
    with open(info_p, encoding="utf-8") as f:
        info = json.load(f)
    for rel in info.get("files", []):
        src = os.path.join(backup, *rel.split("/"))
        if os.path.isfile(src):
            dst = os.path.join(base, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
    for rel in info.get("new", []):  # durch das Update neu hinzugekommene Dateien entfernen
        if rel not in info.get("files", []):
            try:
                os.remove(os.path.join(base, *rel.split("/")))
            except OSError:
                pass
    return True
