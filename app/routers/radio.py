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
import logging
import httpx
from fastapi import APIRouter, Query

router = APIRouter()
logger = logging.getLogger(__name__)

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

GRID = 0.5  # 도 단위, 적도 기준 약 55km

# ---------------------------------------------------------------------------
# 지역 확충: 글로벌 top-voted 쿼리만으로는 인기 방송국이 적은 지역
# (한국/일본/호주 제외 대부분 소규모 섬 지역)이 거의 안 잡히므로,
# 지역별로 별도 조회해서 강제로 클러스터에 포함시킨다.
#
# bbox = (lat_min, lat_max, lng_min, lng_max). 국가 전체가 대상 지역인
# 경우(한국/일본/호주)는 countrycode만으로 충분해서 bbox=None.
# 국가 안의 특정 지역만 원하는 경우(하와이/발리)는 countrycode로 1차
# 후보를 좁힌 뒤 bbox로 한 번 더 필터링한다 (state 메타데이터가
# 부정확한 방송국이 많아서 좌표 검증이 필요함).
# ---------------------------------------------------------------------------
REGION_QUERIES = [
    {"name": "Korea", "countrycode": "KR", "bbox": None, "limit": 200},
    {"name": "Japan", "countrycode": "JP", "bbox": None, "limit": 250},
    {"name": "Australia", "countrycode": "AU", "bbox": None, "limit": 200},
    {
        "name": "Hawaii",
        "countrycode": "US",
        "bbox": (18.5, 22.5, -160.5, -154.5),
        "limit": 150,
    },
    {"name": "Guam", "countrycode": "GU", "bbox": None, "limit": 50},
    {
        "name": "Saipan (N. Mariana Islands)",
        "countrycode": "MP",
        "bbox": None,
        "limit": 50,
    },
    {
        "name": "Bali",
        "countrycode": "ID",
        "bbox": (-9.0, -8.0, 114.3, 115.8),
        "limit": 200,
    },
]


def _in_bbox(lat: float, lng: float, bbox) -> bool:
    if bbox is None:
        return True
    lat_min, lat_max, lng_min, lng_max = bbox
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _fetch_stations(client: httpx.Client, countrycode: str, limit: int) -> list:
    params = {
        "has_geo_info": "true",
        "hidebroken": "true",
        "order": "votes",
        "reverse": "true",
        "limit": limit,
        "countrycode": countrycode,
    }
    res = client.get(f"{RADIO_BROWSER_HOST}/json/stations/search", params=params)
    res.raise_for_status()
    return res.json()


def _add_to_clusters(clusters: dict, stations: list, bbox=None):
    for s in stations:
        lat, lng = s.get("geo_lat"), s.get("geo_long")
        if lat is None or lng is None:
            continue
        if not _in_bbox(lat, lng, bbox):
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


@router.get("/markers")
def radio_markers(limit: int = 800):
    """
    지구본에 찍을 '라디오 도시' 마커 목록.
    전세계 인기 방송국을 좌표 격자(0.5도)로 묶어서 도시 단위 클러스터로 반환한다.
    Radio Browser API 부하를 줄이기 위해 30분간 캐시한다.

    1) 글로벌 top-voted 방송국으로 기본 클러스터를 만들고
    2) REGION_QUERIES에 정의된 지역들을 별도로 조회해서 보강한다.
       (인기순 글로벌 쿼리만으로는 소규모 지역이 거의 안 잡히기 때문)
    """
    now = time.time()
    if _markers_cache["data"] is not None and (now - _markers_cache["ts"]) < MARKERS_CACHE_TTL:
        return _markers_cache["data"]

    clusters: dict[tuple[float, float], dict] = {}

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15) as client:
        # 1) 글로벌 top-voted
        try:
            global_params = {
                "has_geo_info": "true",
                "hidebroken": "true",
                "order": "votes",
                "reverse": "true",
                "limit": limit,
            }
            res = client.get(f"{RADIO_BROWSER_HOST}/json/stations/search", params=global_params)
            res.raise_for_status()
            _add_to_clusters(clusters, res.json())
        except httpx.HTTPError as e:
            logger.warning("radio markers: global query failed: %s", e)

        # 2) 지역 확충 (한국/일본/호주/하와이/괌/사이판/발리)
        for region in REGION_QUERIES:
            try:
                stations = _fetch_stations(client, region["countrycode"], region["limit"])
                _add_to_clusters(clusters, stations, bbox=region["bbox"])
            except httpx.HTTPError as e:
                logger.warning("radio markers: region '%s' query failed: %s", region["name"], e)
                continue

    result = list(clusters.values())
    _markers_cache["data"] = result
    _markers_cache["ts"] = now
    return result
