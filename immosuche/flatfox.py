"""Flatfox: öffentliche Schnittstelle (https://flatfox.ch/api/v1/)."""
from __future__ import annotations

import logging
import math
import re

import requests

from .models import Listing

API = "https://flatfox.ch/api/v1"
BASE = "https://flatfox.ch"
GEO = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
HEADERS = {"User-Agent": "immosuche/1.0 (private Suchbenachrichtigung)"}
MAX_PINS = 1000
log = logging.getLogger(__name__)


def geocode(name: str, session: requests.Session) -> tuple[float, float]:
    """Ortszentrum über die Suchschnittstelle von geo.admin.ch (swisstopo)."""
    for origins in ("gg25", "zipcode", "gazetteer"):
        r = session.get(GEO, params={"searchText": name, "type": "locations",
                                     "origins": origins, "sr": 4326}, timeout=30)
        r.raise_for_status()
        res = r.json().get("results") or []
        if res:
            a = res[0]["attrs"]
            return float(a["lat"]), float(a["lon"])
    raise ValueError(f"Ort nicht gefunden: {name}")


def bbox(lat: float, lon: float, km: float) -> dict:
    dlat = km / 111.0
    dlon = km / (111.0 * math.cos(math.radians(lat)))
    return {"north": lat + dlat, "south": lat - dlat, "east": lon + dlon, "west": lon - dlon}


def distance_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _pins(session, box: dict, params: dict, depth: int = 0) -> list[dict]:
    r = session.get(f"{API}/pin/", params={**box, **params, "max_count": MAX_PINS}, timeout=60)
    r.raise_for_status()
    pins = r.json()
    if len(pins) >= MAX_PINS and depth < 5:  # Ausschnitt zu gross: in vier Teile aufteilen
        mid_lat = (box["north"] + box["south"]) / 2
        mid_lon = (box["east"] + box["west"]) / 2
        out: dict[int, dict] = {}
        for n, s in ((box["north"], mid_lat), (mid_lat, box["south"])):
            for e, w in ((box["east"], mid_lon), (mid_lon, box["west"])):
                for p in _pins(session, {"north": n, "south": s, "east": e, "west": w}, params, depth + 1):
                    out[p["pk"]] = p
        return list(out.values())
    return pins


def _details(session, pks: list[int]) -> list[dict]:
    out = []
    for i in range(0, len(pks), 50):
        chunk = pks[i:i + 50]
        params = [("pk", pk) for pk in chunk] + [("expand", "cover_image"), ("limit", 50)]
        r = session.get(f"{API}/public-listing/", params=params, timeout=60)
        r.raise_for_status()
        out.extend(r.json()["results"])
    return out


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_listing(d: dict) -> Listing:
    price = _f(d.get("rent_gross")) or _f(d.get("price_display")) or _f(d.get("selling_price"))
    img = (d.get("cover_image") or {}) if isinstance(d.get("cover_image"), dict) else {}
    title = d.get("description_title") or d.get("public_title") or d.get("short_title") or ""
    l = Listing(
        source="Flatfox", key=str(d["pk"]), url=BASE + (d.get("url") or f"/{d['pk']}/").replace("/en/", "/de/", 1),
        title=title.strip(), price=price, rooms=_f(d.get("number_of_rooms")),
        area=_f(d.get("surface_living")) or _f(d.get("space_display")),
        zipcode=str(d.get("zipcode") or ""), city=d.get("city") or "", street=d.get("street") or "",
        image=(BASE + img["url_thumb_m"]) if img.get("url_thumb_m") else "",
        lat=_f(d.get("latitude")), lon=_f(d.get("longitude")),
        available=d.get("moving_date") or ({"imm": "sofort", "agr": "nach Vereinbarung"}
                                           .get(d.get("moving_date_type") or "", "")),
    )
    if d.get("smg_id"):
        l.extra_ids.add(f"smg:{d['smg_id']}")
    return l


def matches(d: dict, crit: dict) -> bool:
    if d.get("status") != "act" or d.get("reserved"):
        return False
    if crit.get("kategorien") and d.get("object_category") not in crit["kategorien"]:
        return False
    l = to_listing(d)

    def within(v, lo, hi):
        if v is None:
            return lo is None and hi is None  # ohne Angabe nur, wenn kein Filter gesetzt
        return (lo is None or v >= lo) and (hi is None or v <= hi)

    if not within(l.price, crit.get("preis_min"), crit.get("preis_max")):
        return False
    if not within(l.rooms, crit.get("zimmer_min"), crit.get("zimmer_max")):
        return False
    if not within(l.area, crit.get("flaeche_min"), crit.get("flaeche_max")):
        return False
    attrs = {a.get("name") for a in d.get("attributes") or []}
    if not set(crit.get("merkmale_pflicht") or []) <= attrs:
        return False
    text = " ".join(str(d.get(k) or "") for k in ("description_title", "public_title", "description")).lower()
    if any(re.search(r"(?<!\w)" + re.escape(w.lower()), text) for w in crit.get("ausschluss_woerter") or []):
        return False
    return True


def search(crit: dict, rejected_before: set[str]) -> tuple[list[Listing], set[str]]:
    """Liefert (alle aktuell passenden Inserate, neu verworfene uids).

    `rejected_before` = früher bereits als unpassend erkannte uids (werden nicht erneut geladen).
    """
    s = requests.Session()
    s.headers.update(HEADERS)
    params = {"offer_type": crit.get("angebot") or "RENT"}
    if crit.get("preis_min") is not None:
        params["min_price"] = crit["preis_min"]
    if crit.get("preis_max") is not None:
        params["max_price"] = crit["preis_max"]

    candidates: dict[int, dict] = {}
    for ort in crit.get("orte") or []:
        if ort.get("lat") is not None and ort.get("lon") is not None:
            lat, lon = float(ort["lat"]), float(ort["lon"])
        else:
            lat, lon = geocode(ort["name"], s)
        ort.setdefault("lat", round(lat, 6)); ort.setdefault("lon", round(lon, 6))  # für die Karte
        km = float(ort.get("umkreis_km", 3))
        for p in _pins(s, bbox(lat, lon, km), params):
            if p.get("latitude") and distance_km(lat, lon, p["latitude"], p["longitude"]) <= km:
                candidates[p["pk"]] = p
        log.info("Flatfox %s (+%s km): %d Kandidaten bisher", ort.get("name"), km, len(candidates))

    pks = [pk for pk in candidates if f"flatfox:{pk}" not in rejected_before]
    details = _details(s, pks)
    hits = [to_listing(d) for d in details if matches(d, crit)]
    rejected = {f"flatfox:{d['pk']}" for d in details if not matches(d, crit)}
    log.info("Flatfox: %d Kandidaten geprüft, %d passend", len(pks), len(hits))
    return hits, rejected
