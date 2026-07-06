"""
Radio Browser (radio-browser.info) integration.

This is a free, open, community-run directory of internet radio
streams — no API key needed. We query it for stations near a given
city and sort by distance, since the API itself doesn't offer a
clean "stations near this city" filter.

Etiquette from their docs: always send a descriptive User-Agent so
they can see which apps use the service.
"""
import time
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

_markers_cache: dict = {"data": None, "ts": 0}
MARKERS_CACHE_TTL = 60 * 30  # 30분 캐시


@router.get("/markers")
def radio_markers(limit: int = 800):
    """
    지구본에 찍을 '라디오 도시' 마커 목록.
    전세계 인기 방송국을 좌표 격자(0.5도)로 묶어서 도시 단위 클러스터로 반환한다.
    Radio Browser API 부하를 줄이기 위해 30분간 캐시한다.
    """
    now = time.time()
    if _markers_cache["data"] is not None and (now - _markers_cache["ts"]) < MARKERS_CACHE_TTL:
        return _markers_cache["data"]

    params = {
        "has_geo_info": "true",
        "hidebroken": "true",
        "order": "votes",
        "reverse": "true",
        "limit": limit,
    }
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15) as client:
        res = client.get(f"{RADIO_BROWSER_HOST}/json/stations/search", params=params)
        res.raise_for_status()
        stations = res.json()

    clusters: dict[tuple[float, float], dict] = {}
    GRID = 0.5  # 도 단위, 적도 기준 약 55km

    for s in stations:
        lat, lng = s.get("geo_lat"), s.get("geo_long")
        if lat is None or lng is None:
            continue
        key = (round(lat / GRID) * GRID, round(lng / GRID) * GRID)
        if key not in clusters:
            clusters[key] = {
                "lat": lat,
                "lng": lng,
                "country": s.get("country", ""),
                "count": 1,
                "top_station": {
                    "stationuuid": s["stationuuid"],
                    "name": s["name"],
                    "url": s.get("url_resolved") or s["url"],
                    "favicon": s.get("favicon") or None,
                },
            }
        else:
            clusters[key]["count"] += 1
            # 이미 votes 내림차순으로 정렬돼 있으므로 처음 잡힌 방송국이 top_station으로 유지됨

    result = list(clusters.values())
    _markers_cache["data"] = result
    _markers_cache["ts"] = now
    return result    