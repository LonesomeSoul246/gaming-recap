"""
Spieldauern ("Time to Beat") über die offizielle IGDB-API (Twitch).

Früher wurde HowLongToBeat abgefragt. HLTB schützt seine Suche inzwischen gegen
automatische Abfragen, deshalb nutzen wir die offizielle IGDB-Schnittstelle.
Zugang: kostenlos über https://dev.twitch.tv/console (Client-ID + Client-Secret).
Zeiten lassen sich pro Spiel auch von Hand eintragen.
Die Tabelle heißt aus Kompatibilitätsgründen weiter "hltb".
"""
import difflib
import re
import sqlite3
import time
import urllib.error
import urllib.parse

from common import http_json, norm_title

IGDB = "https://api.igdb.com/v4"
_token = {"value": None, "exp": 0, "cid": None}


def init_db(con):
    con.execute("CREATE TABLE IF NOT EXISTS hltb (key TEXT PRIMARY KEY, hltb_id INTEGER, name TEXT, main INTEGER, "
                "plus INTEGER, comp INTEGER, image TEXT, fetched INTEGER, manual INTEGER DEFAULT 0, error TEXT)")
    if "critic" not in [r[1] for r in con.execute("PRAGMA table_info(hltb)")]:
        con.execute("ALTER TABLE hltb ADD COLUMN critic INTEGER")


def _token_for(cid, secret):
    if _token["value"] and _token["cid"] == cid and time.time() < _token["exp"] - 300:
        return _token["value"]
    try:
        j = http_json("https://id.twitch.tv/oauth2/token?" + urllib.parse.urlencode(
            {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}), data=b"", method="POST")
    except urllib.error.HTTPError as e:
        raise RuntimeError("Twitch lehnt Client-ID oder Client-Secret ab. Prüfe die IGDB-Zugangsdaten.") from e
    _token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)), cid=cid)
    return _token["value"]


def igdb(cid, secret, endpoint, query):
    tok = _token_for(cid, secret)
    return http_json(f"{IGDB}/{endpoint}", data=query.encode("utf-8"), method="POST", timeout=20,
                     headers={"Client-ID": cid, "Authorization": "Bearer " + tok, "Content-Type": "text/plain"})


def _clean(title):
    t = re.sub(r"[™®©℠]", "", title or "")
    t = re.sub(r"\s*[-–(]\s*(pc edition|windows( edition)?|xbox one|xbox series x\|s|for windows 10|game of the year edition|goty edition|definitive edition|remastered|enhanced( edition)?|steam edition)\)?\s*$", "", t, flags=re.I)
    return t.replace('"', "").strip()


def _variants(title):
    """Suchvarianten: voller Titel, ohne Untertitel, Teil nach dem Doppelpunkt."""
    q = _clean(title)
    out = [q]
    for sep in (":", " - ", " – "):
        if sep in q:
            a, b = q.split(sep, 1)
            out += [a.strip(), b.strip()]
    out.append(re.sub(r"\s+(steam edition|game of the year|deluxe|ultimate|gold|complete)( edition)?$", "", q, flags=re.I))
    seen, res = set(), []
    for v in out:
        if len(v) >= 3 and v.lower() not in seen:
            seen.add(v.lower())
            res.append(v)
    return res


def _ttb(cid, secret, ids):
    """Spieldauern für mehrere IGDB-IDs. Fehlt eine, wird die des Hauptspiels genommen (Editionen)."""
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        res = igdb(cid, secret, "game_time_to_beats",
                   f"fields game_id,hastily,normally,completely; where game_id = ({','.join(map(str, chunk))}); limit 500;")
        for t in res or []:
            out[t.get("game_id")] = t
    return out


def _pack(g, t):
    mins = lambda v: round(v / 60) if v else None  # Sekunden -> Minuten
    img = (g.get("cover") or {}).get("image_id")
    t = t or {}
    return {"hltb_id": g["id"], "name": g.get("name"),
            "main": mins(t.get("hastily")) or mins(t.get("normally")), "plus": mins(t.get("normally")),
            "comp": mins(t.get("completely")),
            "critic": round(g["aggregated_rating"]) if g.get("aggregated_rating") and (g.get("aggregated_rating_count") or 0) >= 3 else None,
            "image": f"https://images.igdb.com/igdb/image/upload/t_cover_big/{img}.jpg" if img else None}


GFIELDS = "fields id,name,cover.image_id,url,category,version_parent,parent_game,aggregated_rating,aggregated_rating_count"


def by_steam(appids, cid, secret):
    """Exakte Zuordnung über die Steam-ID (IGDB kennt sie für fast alle Spiele). {appid: ergebnis}"""
    appids = [str(a) for a in appids if str(a).isdigit()]
    if not appids:
        return {}
    gid_of = {}
    for i in range(0, len(appids), 300):
        chunk = appids[i:i + 300]
        uids = ",".join(f'"{a}"' for a in chunk)
        res = None
        for where in (f"uid = ({uids}) & external_game_source = 1", f"uid = ({uids}) & category = 1"):
            try:
                res = igdb(cid, secret, "external_games", f"fields game,uid; where {where}; limit 500;")
                break
            except urllib.error.HTTPError:
                continue
        for e in res or []:
            if e.get("game") and e.get("uid"):
                gid_of.setdefault(str(e["uid"]), e["game"])
        time.sleep(0.3)
    if not gid_of:
        return {}
    ids = sorted(set(gid_of.values()))
    games = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for g in igdb(cid, secret, "games", f"{GFIELDS}; where id = ({','.join(map(str, chunk))}); limit 500;") or []:
            games[g["id"]] = g
        time.sleep(0.3)
    ttb = _ttb(cid, secret, ids)
    # Editionen ohne eigene Zeit: Zeit des Hauptspiels
    parents = {g.get("version_parent") or g.get("parent_game") for g in games.values()
               if g["id"] not in ttb and (g.get("version_parent") or g.get("parent_game"))}
    ttb.update(_ttb(cid, secret, sorted(p for p in parents if p)))
    out = {}
    for a, gid in gid_of.items():
        g = games.get(gid)
        if g:
            t = ttb.get(gid) or ttb.get(g.get("version_parent")) or ttb.get(g.get("parent_game"))
            out[a] = _pack(g, t)
    return out


def search(title, cid, secret):
    """Bestes IGDB-Ergebnis per Name (mehrere Varianten), oder None wenn nichts passt."""
    for q in _variants(title):
        res = igdb(cid, secret, "games", f'search "{q}"; {GFIELDS}; limit 10;')
        nt = norm_title(q)
        best, score = None, 0.0
        for g in res or []:
            s = difflib.SequenceMatcher(None, nt, norm_title(g.get("name"))).ratio()
            if g.get("version_parent"):
                s -= 0.05  # Editionen leicht abwerten, Hauptspiel bevorzugen
            if s > score:
                best, score = g, s
        if best and score >= 0.8:
            t = _ttb(cid, secret, [best["id"]]).get(best["id"])
            par = best.get("version_parent") or best.get("parent_game")
            if not t and par:
                t = _ttb(cid, secret, [par]).get(par)
            return _pack(best, t)
        time.sleep(0.25)
    return None


def load(db_path):
    con = sqlite3.connect(db_path, timeout=10)
    init_db(con)
    rows = con.execute("SELECT key, hltb_id, name, main, plus, comp, image, manual, critic FROM hltb "
                       "WHERE main IS NOT NULL OR image IS NOT NULL OR manual=1 OR critic IS NOT NULL").fetchall()
    con.close()
    return {k: {"id": i, "name": n, "main": m, "plus": p, "comp": c, "image": img, "manual": bool(man), "critic": cr}
            for k, i, n, m, p, c, img, man, cr in rows}


def set_manual(db_path, key, main_minutes):
    con = sqlite3.connect(db_path, timeout=10)
    init_db(con)
    with con:
        if main_minutes is None:
            con.execute("DELETE FROM hltb WHERE key=?", (key,))
        elif con.execute("SELECT key FROM hltb WHERE key=?", (key,)).fetchone():
            con.execute("UPDATE hltb SET main=?, manual=1 WHERE key=?", (main_minutes, key))
        else:
            con.execute("INSERT INTO hltb (key, main, manual, fetched) VALUES (?, ?, 1, ?)", (key, main_minutes, int(time.time())))
    con.close()


def update(db_path, wanted, cid, secret, limit=250, delay=0.3, status=None):
    """wanted: Liste (key, titel, steam_appid|None). Holt fehlende Einträge nach. Gibt (neu, fehler, rest) zurück.
    Erst exakt über die Steam-ID (in Sammelabfragen), dann per Name."""
    con = sqlite3.connect(db_path, timeout=10)
    init_db(con)
    if "v" not in [r[1] for r in con.execute("PRAGMA table_info(hltb)")]:
        con.execute("ALTER TABLE hltb ADD COLUMN v INTEGER DEFAULT 1")
    known = {k: (f or 0, m, main, v) for k, f, m, main, v in con.execute("SELECT key, fetched, manual, main, v FROM hltb")}
    now = int(time.time())

    def need(k):
        if k not in known:
            return True
        f, man, main, v = known[k]
        if man:
            return False
        if (v or 1) < 2 and not main:  # mit der neuen Suche nochmal versuchen
            return True
        return now - f > 60 * 86400

    todo = [w for w in wanted if need(w[0])]
    if status is not None:
        status["total"] = min(len(todo), limit + len([w for w in todo if len(w) > 2 and w[2]]))
    save = lambda key, r, ver=2: con.execute(
        "INSERT OR REPLACE INTO hltb (key, hltb_id, name, main, plus, comp, image, fetched, manual, critic, v) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (key, *(r[k] for k in ("hltb_id", "name", "main", "plus", "comp", "image")), now, r.get("critic"), ver) if r
        else (key, None, None, None, None, None, None, now, None, ver))
    new = 0
    got = set()
    # 1) Steam-IDs gesammelt
    steam = [w for w in todo if len(w) > 2 and w[2]]
    if steam:
        try:
            res = by_steam([w[2] for w in steam], cid, secret)
        except RuntimeError:
            con.close()
            raise
        except Exception:
            res = {}
        with con:
            for key, title, appid in steam:
                r = res.get(str(appid))
                if r and (r["main"] or r["image"]):
                    save(key, r)
                    got.add(key)
                    new += 1
                    if status is not None:
                        status["done"] = status.get("done", 0) + 1
        todo = [w for w in todo if not (len(w) > 2 and w[2] and (res.get(str(w[2])) or {}).get("main"))]
        if status is not None:
            status["done"] = len(steam) - len([w for w in todo if len(w) > 2 and w[2]])
            status["total"] = status["done"] + min(len(todo), limit)
    # 2) Rest per Name
    errors = streak = 0
    for w in todo[:limit]:
        key, title = w[0], w[1]
        try:
            r = search(title, cid, secret)
            streak = 0
        except RuntimeError:
            con.close()
            raise
        except Exception as e:
            errors += 1
            streak += 1
            if streak >= 4:
                con.close()
                raise RuntimeError("IGDB gerade nicht erreichbar. Neuer Versuch beim nächsten Abgleich.") from e
            time.sleep(1)
            continue
        with con:
            old = known.get(key)
            if r or not ((old and old[2]) or key in got):  # Vorhandenes nicht durch "nichts gefunden" ersetzen
                save(key, r)
        if r:
            new += 1
        if status is not None:
            status["done"] = status.get("done", 0) + 1
        time.sleep(delay)
    con.close()
    return new, errors, max(0, len(todo) - limit)
