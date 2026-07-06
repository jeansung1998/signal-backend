"""
Radio Browser (radio-browser.info) integration.

This is a free, open, community-run directory of internet radio
streams — no API key needed. We query it for stations near a given
city and sort by distance, since the API itself doesn't offer a
clean "stations near this city" filter.

Etiquette from their docs: always send a descriptive User-Agent so
they can see which apps use the service.
"""
import math
import httpx
from fastapi import APIRouter, Query

router = APIRouter()

RADIO_BROWSER_HOST = "https://de1.api.radio-browser.info"
USER_AGENT = "Signal-App/0.1 (prototype)"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@router.get("/stations")
def stations_near(
    lat: float,
    lng: float,
    countrycode: str | None = Query(None, description="ISO 3166-1 alpha-2, e.g. KR, US, FR"),
    limit: int = 20,
):
    """
    Returns up to `limit` stations, nearest-first, to the given coordinates.
    Narrowing by countrycode first keeps the upstream request small —
    without it we'd have to pull a much bigger slice of the global list.
    """
    params = {
        "has_geo_info": "true",
        "hidebroken": "true",
        "order": "votes",
        "reverse": "true",
        "limit": 300,
    }
    if countrycode:
        params["countrycode"] = countrycode.upper()

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10) as client:
        res = client.get(f"{RADIO_BROWSER_HOST}/json/stations/search", params=params)
        res.raise_for_status()
        candidates = res.json()

    ranked = []
    for s in candidates:
        if s.get("geo_lat") is None or s.get("geo_long") is None:
            continue
        dist = haversine_km(lat, lng, s["geo_lat"], s["geo_long"])
        ranked.append((dist, s))
    ranked.sort(key=lambda x: x[0])

    return [
        {
            "stationuuid": s["stationuuid"],
            "name": s["name"],
            "url": s["url_resolved"] or s["url"],
            "favicon": s.get("favicon") or None,
            "tags": s.get("tags", ""),
            "country": s.get("country", ""),
            "votes": s.get("votes", 0),
            "distance_km": round(dist, 1),
        }
        for dist, s in ranked[:limit]
    ]


@router.post("/click/{station_uuid}")
def register_click(station_uuid: str):
    """
    Tells Radio Browser a station was played. Not required for our app
    to function, but it's how the community keeps station popularity
    rankings meaningful — worth calling whenever the player starts a stream.
    """
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10) as client:
        res = client.get(f"{RADIO_BROWSER_HOST}/json/url/{station_uuid}")
        res.raise_for_status()
        return res.json()