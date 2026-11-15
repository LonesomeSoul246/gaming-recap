"""Zusatzdaten: PlayStation-Trophäen und Gratis-Spiele (Epic Games Store)."""
import time
import urllib.parse

from common import http_json, norm_title


def psn_trophies(token):
    """Trophäen-Übersicht aller PlayStation-Spiele (offizielle App-Schnittstelle, 1–3 Anfragen)."""
    out, offset = [], 0
    while True:
        j = http_json("https://m.np.playstation.com/api/trophy/v1/users/me/trophyTitles?"
                      + urllib.parse.urlencode({"limit": 800, "offset": offset}),
                      headers={"Authorization": "Bearer " + token}, timeout=40)
        items = j.get("trophyTitles") or []
        for t in items:
            d, e = t.get("definedTrophies") or {}, t.get("earnedTrophies") or {}
            out.append({
                "key": norm_title(t.get("trophyTitleName")), "name": t.get("trophyTitleName"),
                "icon": t.get("trophyTitleIconUrl"), "platform": t.get("trophyTitlePlatform"),
                "progress": int(t.get("progress") or 0),
                "def": [int(d.get(k) or 0) for k in ("platinum", "gold", "silver", "bronze")],
                "got": [int(e.get(k) or 0) for k in ("platinum", "gold", "silver", "bronze")],
                "updated": t.get("lastUpdatedDateTime"),
            })
        offset += len(items)
        total = int(j.get("totalItemCount") or 0)
        if not items or offset >= total or offset > 5000:
            break
        time.sleep(0.5)
    return out


def epic_free():
    """Aktuelle und kommende Gratis-Spiele im Epic Games Store."""
    j = http_json("https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?"
                  + urllib.parse.urlencode({"locale": "de", "country": "DE", "allowCountries": "DE"}), timeout=30)
    els = (((j.get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []
    out = []
    for el in els:
        promos = el.get("promotions") or {}
        for kind, field in (("now", "promotionalOffers"), ("soon", "upcomingPromotionalOffers")):
            for block in promos.get(field) or []:
                for o in block.get("promotionalOffers") or []:
                    if int((o.get("discountSetting") or {}).get("discountPercentage", 100)) != 0:
                        continue
                    imgs = {i.get("type"): i.get("url") for i in el.get("keyImages") or [] if i.get("url")}
                    slug = el.get("productSlug") or next((m.get("pageSlug") for m in (el.get("catalogNs") or {}).get("mappings") or []
                                                           if m.get("pageSlug")), None) or el.get("urlSlug")
                    out.append({"title": el.get("title"), "key": norm_title(el.get("title")), "kind": kind,
                                "start": o.get("startDate"), "end": o.get("endDate"),
                                "image": next((imgs[k] for k in ("OfferImageTall", "DieselStoreFrontTall", "Thumbnail", "OfferImageWide",
                                                                 "DieselStoreFrontWide", "VaultClosed") if imgs.get(k)),
                                              next(iter(imgs.values()), None)),
                                "source": "Epic",
                                "url": f"https://store.epicgames.com/de/p/{slug}" if slug else "https://store.epicgames.com/de/free-games"})
    seen, uniq = set(), []
    for x in out:
        if (x["key"], x["kind"]) not in seen:
            seen.add((x["key"], x["kind"]))
            uniq.append(x)
    return uniq


def _clean(title):
    import re
    t = re.sub(r"\s*\((?:[^)]*)\)\s*Giveaway\s*$", "", title or "", flags=re.I)
    return re.sub(r"\s*Giveaway\s*$", "", t, flags=re.I).strip()


def gamerpower():
    """Gratis-Spiele von GOG, Epic und Prime Gaming (Amazon Luna) über die offene GamerPower-Liste."""
    import re
    out = []
    lists = []
    for url in ("https://www.gamerpower.com/api/filter?platform=epic-games-store.gog&type=game",
                "https://www.gamerpower.com/api/giveaways?type=game&platform=pc"):
        try:
            j = http_json(url, timeout=30)
            if isinstance(j, list):
                lists.append(j)
        except Exception:
            pass
        time.sleep(0.5)
    for lst in lists:
        for g in lst:
            title, plats = g.get("title") or "", (g.get("platforms") or "")
            text = f"{title} {g.get('description') or ''} {g.get('instructions') or ''}"
            prime = bool(re.search(r"prime gaming|amazon|luna", text, re.I))
            if "GOG" in plats or re.search(r"\bGOG\b", title):
                src = "GOG"
            elif "Epic" in plats or "Epic" in title:
                src = "Epic"
            else:
                src = None
            if prime:
                src = "Prime Gaming" + (f" ({src})" if src else "")
            if not src:
                continue
            end = g.get("end_date")
            name = _clean(title)
            out.append({"title": name, "key": norm_title(name), "kind": "now", "start": g.get("published_date"),
                        "end": None if not end or end == "N/A" else end.replace(" ", "T"),
                        "image": g.get("thumbnail") or g.get("image"), "source": src,
                        "url": g.get("open_giveaway_url") or g.get("gamerpower_url"), "worth": g.get("worth")})
    return out


def free_games():
    """Epic (inkl. Vorschau) + GOG + Prime Gaming. Doppelte zusammenführen, Epic-Daten haben Vorrang."""
    out, seen = [], set()
    try:
        for x in epic_free():
            out.append(x)
            seen.add((x["key"], x["kind"]))
    except Exception as e:
        print("  Epic-Gratisspiele:", e)
    try:
        for x in gamerpower():
            if (x["key"], x["kind"]) in seen:
                for y in out:  # Epic-Eintrag: Prime-Hinweis ergänzen
                    if y["key"] == x["key"] and "Prime" in x["source"] and "Prime" not in y.get("source", ""):
                        y["source"] += " · auch Prime Gaming"
                continue
            seen.add((x["key"], x["kind"]))
            out.append(x)
    except Exception as e:
        print("  GamerPower:", e)
    return out
