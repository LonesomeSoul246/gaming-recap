"""
Steam-Extras: Erfolge, Wunschliste mit Preisen, Kontowert und Freunde.
Alles wird in daten/steam_extra.json zwischengespeichert, damit die Steam-API
nicht bei jedem Abgleich hunderte Male gefragt wird.
"""
import json
import time
import urllib.error
import urllib.parse

from common import http_json, read_json, write_json

API = "https://api.steampowered.com"
DAY = 86400


def _get(path, **params):
    return http_json(f"{API}{path}?" + urllib.parse.urlencode(params), timeout=30)


# ---------------------------------------------------------------- Store-Daten (Preise, Namen, Release)

def store_items(appids, cc="DE", lang="german"):
    """Holt Name, Preis, Rabatt und Erscheinungsdatum für viele Apps (50 pro Anfrage)."""
    out = {}
    ids = [int(a) for a in appids if str(a).isdigit()]
    for i in range(0, len(ids), 50):
        req = {"ids": [{"appid": a} for a in ids[i:i + 50]],
               "context": {"language": lang, "country_code": cc, "steam_realm": 1},
               "data_request": {"include_basic_info": True, "include_release": True, "include_assets": True}}
        try:
            j = _get("/IStoreBrowseService/GetItems/v1/", input_json=json.dumps(req))
        except Exception:
            continue
        for it in (j.get("response") or {}).get("store_items") or []:
            appid = str(it.get("appid") or it.get("id") or "")
            if not appid:
                continue
            bpo = it.get("best_purchase_option") or {}
            rel = it.get("release") or {}
            assets = it.get("assets") or {}
            fmt_a = assets.get("asset_url_format")
            # Neuere Spiele haben Bildpfade mit Prüfsumme – die liefert Steam hier mit
            mk = lambda f: ("https://shared.akamai.steamstatic.com/store_item_assets/" + fmt_a.replace("${FILENAME}", f)) if fmt_a and f else None
            num = lambda v: int(v) if str(v or "").lstrip("-").isdigit() else None
            out[appid] = {
                "name": it.get("name"),
                "price": num(bpo.get("final_price_in_cents")),
                "orig": num(bpo.get("original_price_in_cents")) or num(bpo.get("final_price_in_cents")),
                "discount": int(bpo.get("discount_pct") or 0),
                "fmt": bpo.get("formatted_final_price"),
                "free": bool(it.get("is_free")),
                "release": num(rel.get("steam_release_date")) or num(rel.get("original_release_date")),
                "soon": bool(rel.get("is_coming_soon")),
                "release_text": rel.get("custom_release_date_message"),
                "cover": mk(assets.get("library_capsule")), "header": mk(assets.get("header")) or mk(assets.get("main_capsule")),
                "t": int(time.time()),
            }
    return out


# ---------------------------------------------------------------- Erfolge

def achievements(key, sid, appid):
    try:
        j = _get("/ISteamUserStats/GetPlayerAchievements/v1/", key=key, steamid=sid, appid=appid, l="german")
    except urllib.error.HTTPError as e:
        if e.code in (400, 403):  # Spiel ohne Erfolge oder privat
            return {"t": 0, "d": 0}
        raise
    ach = (j.get("playerstats") or {}).get("achievements") or []
    done = [a for a in ach if a.get("achieved")]
    return {"t": len(ach), "d": len(done),
            "u": [[a.get("apiname"), a.get("name") or a.get("apiname"), int(a.get("unlocktime") or 0)] for a in done]}


def global_pct(appid):
    try:
        j = _get("/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/", gameid=appid)
    except Exception:
        return {}
    out = {}
    for a in (j.get("achievementpercentages") or {}).get("achievements") or []:
        try:
            out[a["name"]] = round(float(a.get("percent")), 2)
        except (TypeError, ValueError, KeyError):
            pass
    return out


# ---------------------------------------------------------------- Freunde

def friends(key, sid, own_games, limit=60):
    try:
        fl = _get("/ISteamUser/GetFriendList/v1/", key=key, steamid=sid, relationship="friend")
    except urllib.error.HTTPError:
        return []
    ids = [f["steamid"] for f in (fl.get("friendslist") or {}).get("friends") or []][:limit]
    if not ids:
        return []
    names = {}
    for i in range(0, len(ids), 100):
        j = _get("/ISteamUser/GetPlayerSummaries/v2/", key=key, steamids=",".join(ids[i:i + 100]))
        for p in (j.get("response") or {}).get("players") or []:
            names[p["steamid"]] = (p.get("personaname"), p.get("avatarfull") or p.get("avatarmedium"))
    own = {str(g.get("steam_appid")): g for g in own_games if g.get("steam_appid")}
    out = []
    for fid in ids:
        try:
            j = _get("/IPlayerService/GetOwnedGames/v1/", key=key, steamid=fid, include_appinfo=1, include_played_free_games=1)
        except Exception:
            continue
        games = (j.get("response") or {}).get("games")
        if not games:
            continue  # privates Profil
        total = sum(int(g.get("playtime_forever") or 0) for g in games)
        top = max(games, key=lambda g: g.get("playtime_forever") or 0)
        tops = [[g.get("name"), int(g.get("playtime_forever") or 0), g.get("appid")]
                for g in sorted(games, key=lambda g: -(g.get("playtime_forever") or 0))[:6] if g.get("playtime_forever")]
        recent_g = [[g.get("name"), int(g.get("playtime_2weeks") or 0), g.get("appid")]
                    for g in sorted(games, key=lambda g: -(g.get("playtime_2weeks") or 0))[:3] if g.get("playtime_2weeks")]
        common = []
        for g in games:
            a = str(g.get("appid"))
            if a in own and (g.get("playtime_forever") or 0) > 0 and (own[a].get("minutes") or 0) > 0:
                common.append([own[a]["title"], int(g.get("playtime_forever") or 0), int(own[a]["minutes"])])
        common.sort(key=lambda c: -(c[1] + c[2]))
        recent = sum(int(g.get("playtime_2weeks") or 0) for g in games)
        name, avatar = names.get(fid, (None, None))
        out.append({"id": fid, "name": name or "Unbekannt", "avatar": avatar, "minutes": total, "games": len(games),
                    "top": [top.get("name"), int(top.get("playtime_forever") or 0)], "recent": recent, "common": common[:12],
                    "tops": tops, "recent_games": recent_g, "played": sum(1 for g in games if g.get("playtime_forever"))})
        time.sleep(0.3)
    out.sort(key=lambda f: -f["minutes"])
    return out


# ---------------------------------------------------------------- Gesamter Abgleich

def update(cache_path, key, sid, games, max_ach=120, status=None):
    """games: zusammengeführte Spieleliste. Aktualisiert den Cache schrittweise."""
    c = read_json(cache_path, {})
    c.setdefault("prices", {})
    c.setdefault("ach", {})
    c.setdefault("gpct", {})
    now = int(time.time())
    steam = [g for g in games if g.get("steam_appid")]

    # Wunschliste
    try:
        wl = _get("/IWishlistService/GetWishlist/v1/", steamid=sid, key=key)
        items = (wl.get("response") or {}).get("items") or []
        c["wishlist"] = [{"appid": str(i.get("appid")), "prio": i.get("priority"), "added": i.get("date_added")} for i in items]
        c["wishlist_error"] = None
    except Exception as e:
        c["wishlist_error"] = "Wunschliste nicht lesbar (ist sie öffentlich?)"
        c.setdefault("wishlist", [])

    # Preise für Bibliothek (Kontowert) alle 7 Tage, Wunschliste täglich
    wl_ids = [w["appid"] for w in c.get("wishlist", [])]
    stale = [a for a in wl_ids if now - (c["prices"].get(a, {}).get("t") or 0) > DAY or "cover" not in c["prices"].get(a, {})]
    stale += [g["steam_appid"] for g in steam if now - (c["prices"].get(g["steam_appid"], {}).get("t") or 0) > 7 * DAY]
    if stale:
        old = {a: dict(c["prices"][a]) for a in wl_ids if a in c["prices"]}
        c["prices"].update(store_items(list(dict.fromkeys(stale))))
        # Neue Rabatte auf der Wunschliste merken (für Benachrichtigungen)
        c["new_sales"] = [a for a in wl_ids if (c["prices"].get(a, {}).get("discount") or 0) > 0
                          and (old.get(a, {}).get("discount") or 0) == 0]

    # Erfolge: nur gespielte Spiele, neu laden wenn seitdem gespielt wurde
    played = sorted([g for g in steam if (g.get("minutes") or 0) > 0], key=lambda g: g.get("last_played") or "", reverse=True)
    calls = 0
    for g in played:
        a = g["steam_appid"]
        cur = c["ach"].get(a)
        if cur and cur.get("lp") == g.get("last_played") and now - (cur.get("at") or 0) < 30 * DAY:
            continue
        if calls >= max_ach:
            break
        calls += 1
        try:
            r = achievements(key, sid, a)
        except Exception:
            continue
        r.update(lp=g.get("last_played"), at=now)
        if r.get("t") and (a not in c["gpct"] or now - c["gpct"][a].get("at", 0) > 30 * DAY):
            c["gpct"][a] = {"at": now, "p": global_pct(a)}
        c["ach"][a] = r
        if status is not None:
            status["ach"] = status.get("ach", 0) + 1
        time.sleep(0.2)

    # Freunde einmal am Tag
    if now - (c.get("friends_at") or 0) > DAY or any("tops" not in f for f in c.get("friends") or []):
        try:
            c["friends"] = friends(key, sid, games)
            c["friends_at"] = now
        except Exception:
            c.setdefault("friends", [])
    c["at"] = now
    write_json(cache_path, c)
    return c


# ---------------------------------------------------------------- Wertungen (ohne API-Key)

STORE = "https://store.steampowered.com"


def review_summaries(appids):
    """Steam-Nutzerrezensionen über die Store-Schnittstelle (50 Spiele pro Anfrage, kein Key nötig)."""
    out = {}
    ids = [int(a) for a in appids if str(a).isdigit()]
    for i in range(0, len(ids), 50):
        req = {"ids": [{"appid": a} for a in ids[i:i + 50]],
               "context": {"language": "german", "country_code": "DE", "steam_realm": 1},
               "data_request": {"include_reviews": True}}
        j = _get("/IStoreBrowseService/GetItems/v1/", input_json=json.dumps(req))
        for it in (j.get("response") or {}).get("store_items") or []:
            r = ((it.get("reviews") or {}).get("summary_filtered") or {})
            if r.get("review_count"):
                out[str(it.get("appid") or it.get("id"))] = {"rp": int(r.get("percent_positive") or 0),
                                                             "rt": int(r["review_count"]), "rs": r.get("review_score")}
        time.sleep(0.5)
    return out


def metacritic(appid):
    """Metascore der Fachpresse, wie ihn der Steam-Store anzeigt."""
    j = http_json(f"{STORE}/api/appdetails?" + urllib.parse.urlencode({"appids": appid, "filters": "metacritic", "cc": "de"}), timeout=20)
    d = (j.get(str(appid)) or {}).get("data") or {}
    m = d.get("metacritic") if isinstance(d, dict) else None
    return int(m["score"]) if m and m.get("score") else None


def update_scores(cache_path, appids, limit=400, delay=1.2, status=None):
    c = read_json(cache_path, {})
    sc = c.setdefault("scores", {})
    now = int(time.time())
    def stale(e):
        age = now - (e.get("t") or 0)
        return age > 14 * DAY or ("rp" not in e and "mc" not in e and age > 3600) or (e.get("v") != 2)
    todo = [a for a in dict.fromkeys(str(x) for x in appids) if stale(sc.get(a, {}))][:limit]
    if not todo:
        return 0
    if status is not None:
        status.update(total=len(todo), done=0)
    # 1) Nutzerrezensionen gesammelt
    revs = review_summaries(todo)
    for a in todo:
        e = {k: v for k, v in sc.get(a, {}).items() if k == "mc"}
        e.update(revs.get(a, {}))
        e["t"] = now
        e["v"] = 2
        sc[a] = e
    write_json(cache_path, c)
    if status is not None:
        status["scores"] = len(revs)
    # 2) Metascore einzeln (langsam, daher nach und nach)
    fails = 0
    for n, a in enumerate(todo, 1):
        try:
            m = metacritic(a)
            if m:
                sc[a]["mc"] = m
            fails = 0
        except ValueError:
            pass
        except Exception as e:
            fails += 1
            if status is not None:
                status["fails"] = status.get("fails", 0) + 1
                status["last_error"] = str(e)[:200]
            if fails >= 8:
                break
            time.sleep(delay * 4)
        if status is not None:
            status["done"] = n
        if n % 25 == 0:
            write_json(cache_path, c)
        time.sleep(delay)
    write_json(cache_path, c)
    return len(todo)


def summary(c, games):
    """Kompakte Daten fürs Frontend."""
    if not c:
        return {}
    titles = {str(g["steam_appid"]): g["title"] for g in games if g.get("steam_appid")}
    ach = {}
    for a, r in (c.get("ach") or {}).items():
        if not r.get("t"):
            continue
        pct = (c.get("gpct", {}).get(a) or {}).get("p") or {}
        rare = None
        years = {}
        for api, name, ts in r.get("u", []):
            if ts:
                y = time.strftime("%Y", time.localtime(ts))
                years[y] = years.get(y, 0) + 1
            p = pct.get(api)
            if p is not None and (rare is None or p < rare[1]):
                rare = [name, p, ts]
        ach[a] = {"d": r["d"], "t": r["t"], "r": rare, "y": years, "last": max([u[2] for u in r.get("u", [])] or [0])}
    prices = {a: {k: p.get(k) for k in ("price", "orig", "discount", "fmt", "free")}
              for a, p in (c.get("prices") or {}).items() if str(a) in titles}
    wishlist = []
    for w in c.get("wishlist") or []:
        p = (c.get("prices") or {}).get(w["appid"], {})
        wishlist.append({"appid": w["appid"], "name": p.get("name") or f"App {w['appid']}", "added": w.get("added"),
                         "prio": w.get("prio"), "price": p.get("price"), "orig": p.get("orig"), "discount": p.get("discount") or 0,
                         "fmt": p.get("fmt"), "free": p.get("free"), "release": p.get("release"), "soon": p.get("soon"),
                         "release_text": p.get("release_text"), "cover": p.get("cover"), "header": p.get("header")})
    scores = {a: {k: v for k, v in e.items() if k not in ("t", "v")} for a, e in (c.get("scores") or {}).items() if str(a) in titles and ("rp" in e or "mc" in e)}
    return {"ach": ach, "prices": prices, "wishlist": wishlist, "wishlist_error": c.get("wishlist_error"),
            "friends": c.get("friends") or [], "at": c.get("at"), "scores": scores}
