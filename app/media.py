"""
Bibliothekswert (IsThereAnyDeal) und fehlende Cover (SteamGridDB).
Beide brauchen einen kostenlosen API-Key, den der Nutzer in den Einstellungen einträgt.
Ergebnisse liegen in daten/media.json.
"""
import json
import time
import urllib.parse

from common import http_json, norm_title, read_json, write_json

ITAD = "https://api.isthereanydeal.com"
SGDB = "https://www.steamgriddb.com/api/v2"
DAY = 86400


# ---------------------------------------------------------------- IsThereAnyDeal

def _itad_post(path, key, body, **params):
    params["key"] = key
    return http_json(f"{ITAD}{path}?" + urllib.parse.urlencode(params), data=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json"}, method="POST", timeout=40)


def _cents(o):
    if not isinstance(o, dict):
        return None
    if isinstance(o.get("amountInt"), int):
        return o["amountInt"]
    try:
        return int(round(float(o.get("amount")) * 100))
    except (TypeError, ValueError):
        return None


STEAM = 61  # Standard: nur Steam-Preise (Einstellung „Nur Steam-Preise“)
MODE = {"steam_only": True}


def _steam_prices(key, ids):
    """Preis heute, Normalpreis und Tiefstpreis je ITAD-ID – nur Steam oder (ausgeschaltet) bester offizieller Store."""
    out = {}
    if MODE["steam_only"]:
        res = _itad_post("/games/prices/v3", key, list(ids), country="DE", shops=str(STEAM))
    else:
        res = _itad_post("/games/prices/v3", key, list(ids), country="DE")
    for p in res if isinstance(res, list) else []:
        deals = p.get("deals") or []
        if MODE["steam_only"]:
            d = next((x for x in deals if (x.get("shop") or {}).get("id") == STEAM), None)
        else:
            d = min(deals, key=lambda x: _cents(x.get("price")) or 10 ** 9) if deals else None
        if not d:
            continue
        r, c = _cents(d.get("regular")), _cents(d.get("price"))
        l = _cents(d.get("storeLow")) if MODE["steam_only"] else _cents(((p.get("historyLow") or {}).get("all")))
        if l is None or (c is not None and c < l):
            l = c
        out[p.get("id")] = {"r": r, "c": c, "l": l, "cut": d.get("cut") or 0, "url": d.get("url"), "shop": (d.get("shop") or {}).get("name"),
                            "lcut": round((1 - l / r) * 100) if r and l is not None else 0}
    return out


def _mode():
    return STEAM if MODE["steam_only"] else 0


def update_prices(cache_path, key, wanted, status=None, max_lookups=400):
    """wanted: {spielschlüssel: (titel, steam_appid oder None)}. Liefert Anzahl neuer Preise."""
    c = read_json(cache_path, {})
    ids, prices = c.setdefault("ids", {}), c.setdefault("p", {})
    now = int(time.time())
    # 1) ITAD-IDs nachschlagen (Fehlschläge nach 14 Tagen neu versuchen)
    need = [k for k in wanted if k not in ids or (not ids[k][0] and now - ids[k][1] > 14 * DAY)][:max_lookups]
    if status is not None:
        status.update(phase="Preise: Spiele zuordnen", done=0, total=len(need))
    steam = [k for k in need if wanted[k][1]]
    for i in range(0, len(steam), 100):
        part = steam[i:i + 100]
        res = _itad_post("/lookup/id/shop/61/v1", key, [f"app/{wanted[k][1]}" for k in part])
        for k in part:
            ids[k] = [res.get(f"app/{wanted[k][1]}") or "", now]
    rest = [k for k in need if not wanted[k][1] or not ids.get(k, [""])[0]]
    for i in range(0, len(rest), 100):
        part = rest[i:i + 100]
        res = _itad_post("/lookup/id/title/v1", key, [wanted[k][0] for k in part])
        for k in part:
            ids[k] = [res.get(wanted[k][0]) or "", now]
    if status is not None:
        status["ids"] = sum(1 for k in wanted if ids.get(k, [""])[0])
    # 2) Preise (alle 7 Tage)
    stale = [k for k in wanted if ids.get(k, [""])[0] and (now - (prices.get(k) or {}).get("t", 0) > 7 * DAY or (prices.get(k) or {}).get("s") != _mode())]
    if status is not None:
        status.update(phase="Preise laden", done=0, total=len(stale))
    new = 0
    for i in range(0, len(stale), 100):
        part = stale[i:i + 100]
        gid = {ids[k][0]: k for k in part}
        res = _steam_prices(key, list(gid))
        seen = set()
        for i2, d in res.items():
            k = gid.get(i2)
            if not k:
                continue
            prices[k] = {"r": d["r"], "c": d["c"], "l": d["l"], "t": now, "s": _mode()}
            seen.add(k)
            new += 1
        for k in part:
            if k not in seen:
                prices[k] = {"r": None, "c": None, "l": None, "t": now, "s": _mode()}
        write_json(cache_path, c)
        if status is not None:
            status["done"] = min(len(stale), i + 100)
        time.sleep(0.5)
    c["prices_at"] = now
    write_json(cache_path, c)
    return new


def update_wishlist(cache_path, key, appids, status=None):
    """Kaufberater: aktueller Bestpreis aller Stores und Tiefstpreis aller Zeiten für die Wunschliste (täglich)."""
    c = read_json(cache_path, {})
    wids, wl = c.setdefault("wids", {}), c.setdefault("wl", {})
    now = int(time.time())
    appids = [str(a) for a in appids if str(a).isdigit()]
    need = [a for a in appids if a not in wids or (not wids[a][0] and now - wids[a][1] > 7 * DAY)]
    for i in range(0, len(need), 100):
        part = need[i:i + 100]
        res = _itad_post("/lookup/id/shop/61/v1", key, [f"app/{a}" for a in part])
        for a in part:
            wids[a] = [res.get(f"app/{a}") or "", now]
    stale = [a for a in appids if wids.get(a, [""])[0] and (now - (wl.get(a) or {}).get("t", 0) > 20 * 3600 or (wl.get(a) or {}).get("s") != _mode())]
    if status is not None:
        status.update(phase="Wunschliste: Preise prüfen", done=0, total=len(stale))
    for i in range(0, len(stale), 100):
        part = stale[i:i + 100]
        gid = {wids[a][0]: a for a in part}
        res = _steam_prices(key, list(gid))
        for i2, d in res.items():
            a = gid.get(i2)
            if not a:
                continue
            wl[a] = dict(d, shop=d.get("shop") or "Steam", lshop="Steam" if MODE["steam_only"] else None, t=now, s=_mode())
        for a in part:  # alte Werte aus dem anderen Modus verwerfen
            if a not in wl or wl[a].get("s") != _mode():
                wl.pop(a, None)
        if status is not None:
            status["done"] = min(len(stale), i + 100)
        time.sleep(0.5)
    write_json(cache_path, c)
    return {a: wl[a] for a in appids if a in wl}


# ---------------------------------------------------------------- SteamGridDB

def _sgdb(path, key, **params):
    url = f"{SGDB}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    return http_json(url, headers={"Authorization": "Bearer " + key}, timeout=25)


def _clean(title):
    t = str(title or "")
    for x in ("(Game Preview)", "(Win)", "(Windows PC)", "for Windows", "Windows", "(Xbox Series X|S & PC)", "for XBOX One",
              "Digital Deluxe", "Classic Edition", "(BATTLEMODE - PC)", "Demo", "Beta"):
        t = t.replace(x, " ")
    return " ".join(t.replace("™", "").replace("®", "").split())


def sgdb_candidates(key, title, limit=8):
    """Sucht ein Spiel und liefert bis zu 8 Cover [(bild, vorschau)] plus den gefundenen Namen."""
    q = _clean(title)
    found = (_sgdb("/search/autocomplete/" + urllib.parse.quote(q, safe=""), key).get("data") or [])
    if not found:
        return None, []
    want = norm_title(q)
    best = next((g for g in found if norm_title(g.get("name")) == want), found[0])
    grids = _sgdb(f"/grids/game/{best['id']}", key, dimensions="600x900,342x482,660x930", types="static",
                  nsfw="false", humor="false").get("data") or []
    return best.get("name"), [[g["url"], g.get("thumb") or g["url"]] for g in grids[:limit] if g.get("url")]


def update_covers(cache_path, key, wanted, status=None, limit=150):
    """wanted: {spielschlüssel: titel} – nur Spiele ohne brauchbares Online-Cover."""
    c = read_json(cache_path, {})
    cov = c.setdefault("covers", {})
    now = int(time.time())
    todo = [k for k in wanted if k not in cov or (not cov[k].get("u") and now - cov[k].get("t", 0) > 7 * DAY)][:limit]
    if status is not None:
        status.update(phase="Cover suchen", done=0, total=len(todo))
    n = 0
    for k in todo:
        try:
            name, cands = sgdb_candidates(key, wanted[k])
        except Exception as e:
            if "401" in str(e) or "403" in str(e):
                raise
            cands, name = [], None
        cov[k] = {"u": cands[0][0] if cands else None, "n": name, "t": now}
        n += bool(cands)
        if status is not None:
            status["done"] = status.get("done", 0) + 1
        time.sleep(0.35)
    if todo:
        write_json(cache_path, c)
    return n


# ---------------------------------------------------------------- Metacritic über RAWG

RAWG = "https://api.rawg.io/api"


def _rawg_clean(title):
    t = _clean(title)
    for x in ("Game of the Year Edition", "GOTY Edition", "Definitive Edition", "Enhanced Edition", "Complete Edition",
              "Deluxe Edition", "Remastered", "Director's Cut"):
        t = t.replace(x, " ")
    return " ".join(t.split()).strip(" -:")


def rawg_metacritic(key, title):
    """Sucht ein Spiel bei RAWG und liefert (metascore, name, slug) – nur bei eindeutigem Treffer."""
    q = _rawg_clean(title)
    want = norm_title(q)
    for precise in ("true", None):
        params = {"key": key, "search": q, "page_size": 8}
        if precise:
            params["search_precise"] = precise
        res = http_json(f"{RAWG}/games?" + urllib.parse.urlencode(params), timeout=25).get("results") or []
        exact = [r for r in res if norm_title(r.get("name")) == want]
        if exact:
            r = max(exact, key=lambda r: (r.get("metacritic") or 0, r.get("added") or 0))
            return r.get("metacritic"), r.get("name"), r.get("slug")
    return None, None, None


def update_metacritic(cache_path, key, wanted, status=None, limit=700):
    """wanted: {spielschlüssel: titel}. Treffer 30 Tage, Fehlschläge 14 Tage gültig."""
    c = read_json(cache_path, {})
    mc = c.setdefault("mc", {})
    now = int(time.time())
    def stale(k):
        e = mc.get(k)
        return not e or now - e.get("t", 0) > (30 if e.get("m") else 14) * DAY
    todo = [k for k in wanted if stale(k)][:limit]
    if status is not None:
        status.update(phase="Metacritic-Wertungen", done=0, total=len(todo))
    n = errs = 0
    for i, k in enumerate(todo, 1):
        try:
            m, name, slug = rawg_metacritic(key, wanted[k])
            mc[k] = {"m": m, "n": name, "s": slug, "t": now}
            n += bool(m)
            errs = 0
        except Exception as e:
            if "401" in str(e) or "403" in str(e):
                raise RuntimeError("RAWG-Key ungültig") from e
            errs += 1
            if errs >= 5:
                raise
        if status is not None:
            status["done"] = i
        if i % 25 == 0:
            write_json(cache_path, c)
        time.sleep(0.25)
    if todo:
        write_json(cache_path, c)
    return n


def summary(cache_path):
    """Kompakt fürs Frontend: Preise je Spiel [normal, heute, Tiefstpreis] in Cent und Cover-URLs."""
    c = read_json(cache_path, {})
    vals = {k: [p.get("r"), p.get("c"), p.get("l")] for k, p in (c.get("p") or {}).items() if p.get("r") or p.get("l")}
    covers = {k: v["u"] for k, v in (c.get("covers") or {}).items() if v.get("u")}
    wl = {a: {k: v.get(k) for k in ("r", "c", "l", "cut", "lcut", "shop", "lshop", "lt", "url")} for a, v in (c.get("wl") or {}).items()}
    mcs = {k: [v["m"], v.get("s")] for k, v in (c.get("mc") or {}).items() if v.get("m")}
    return {"values": vals, "values_at": c.get("prices_at"), "xcovers": covers, "wl_deals": wl, "metacritic": mcs}
