"""
Radio feature — now DB-backed.

Station data is bulk-loaded into the `radio_stations` table by
`radio_ingest.py` (run manually / periodically), instead of calling
Radio Browser live on every request. This avoids the app going empty
whenever Radio Browser (external service) has a hiccup, and is much
faster since it's just a local DB query.

`/click/{station_uuid}` still calls Radio Browser live (best-effort,
non-critical — just tells the community directory a station was played).
"""
import math
import time
import logging

import httpx
from fastapi import APIRouter, Header, Query

from app.database import get_supabase
from app.radio_health import run_health_check_batch
from app.routers.admin import _require_admin

router = APIRouter()
logger = logging.getLogger(__name__)

RADIO_BROWSER_HOST = "https://de1.api.radio-browser.info"
USER_AGENT = "Signal-App/0.1 (prototype)"

GRID = 0.5  # 도 단위, 적도 기준 약 55km (markers 클러스터링용)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_country_code(lat: float, lng: float) -> str | None:
    """
    클릭한 좌표에서 제일 가까운 `places` 테이블 도시를 찾아 그 나라의
    country_code를 반환한다.
    """
    try:
        sb = get_supabase()
        res = sb.table("places").select("country_code, lat, lng").execute()
        rows = res.data or []
    except Exception as e:
        logger.warning("stations_near: places lookup failed: %s", e)
        return None

    best_code = None
    best_dist = float("inf")
    for r in rows:
        if r.get("lat") is None or r.get("lng") is None:
            continue
        d = haversine_km(lat, lng, r["lat"], r["lng"])
        if d < best_dist:
            best_dist = d
            best_code = r.get("country_code")
    return best_code


@router.get("/stations")
def stations_near(
    lat: float,
    lng: float,
    countrycode: str | None = Query(None, description="ISO 3166-1 alpha-2, e.g. KR, US, FR"),
    limit: int = 20,
):
    """
    DB(`radio_stations`)에서 해당 국가의 방송국을 가져와, 주어진 좌표에서
    가까운 순으로 정렬해 반환한다. 국가가 안 넘어오면 `places` 테이블로
    가장 가까운 나라를 추정한다.
    """
    resolved_countrycode = countrycode or _nearest_country_code(lat, lng)

    if not resolved_countrycode:
        return []

    sb = get_supabase()
    res = (
        sb.table("radio_stations")
        .select("stationuuid, name, url, favicon, tags, country, votes, geo_lat, geo_long, is_synthetic_geo")
        .eq("countrycode", resolved_countrycode.upper())
        .eq("is_hidden", False)
        .eq("is_active", True)
        .execute()
    )
    candidates = res.data or []

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
            "url": s["url"],
            "favicon": s.get("favicon"),
            "tags": s.get("tags", ""),
            "country": s.get("country", ""),
            "votes": s.get("votes", 0),
            "distance_km": round(dist, 1),
            "resolved_countrycode": resolved_countrycode,
            "is_synthetic_geo": s.get("is_synthetic_geo", False),
        }
        for dist, s in ranked[:limit]
    ]


@router.post("/click/{station_uuid}")
def register_click(station_uuid: str):
    """
    Radio Browser에 재생 기록을 알려준다 (커뮤니티 인기 순위 유지용, 필수 아님).
    실패해도 앱 동작에 영향 없도록 예외를 삼킨다.
    """
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10) as client:
            res = client.get(f"{RADIO_BROWSER_HOST}/json/url/{station_uuid}")
            res.raise_for_status()
            return res.json()
    except httpx.HTTPError as e:
        logger.warning("radio click registration failed for %s: %s", station_uuid, e)
        return {"ok": False}


_markers_cache: dict = {"data": None, "ts": 0}
MARKERS_CACHE_TTL = 60 * 5  # DB 조회라 부담 적음 — 5분이면 충분 (예전 30분보다 훨씬 짧게)


def _add_to_clusters(clusters: dict, stations: list):
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
                    "url": s["url"],
                    "favicon": s.get("favicon"),
                },
            }
        else:
            clusters[key]["count"] += 1


@router.get("/markers")
def radio_markers():
    """
    지구본에 찍을 '라디오 도시' 마커 목록. DB(`radio_stations`)의 모든
    방송국을 좌표 격자(0.5도)로 묶어서 도시 단위 클러스터로 반환한다.
    5분간 메모리 캐시 (DB 조회 자체가 가벼워서 예전 30분 캐시보다 짧게 잡음).
    """
    now = time.time()
    if _markers_cache["data"] is not None and (now - _markers_cache["ts"]) < MARKERS_CACHE_TTL:
        return _markers_cache["data"]

    sb = get_supabase()

    # PostgREST default caps a single response at 1000 rows. With 9,000+
    # stations in the table, a plain .execute() silently truncates to the
    # first ~1000 rows in DB order — which happened to be almost entirely
    # European countries (alphabetically early country codes), making every
    # other country's markers disappear. Page through with .range() so we
    # actually get everything.
    stations: list = []
    page_size = 900  # PostgREST 기본 캡(1000)보다 여유 있게 낮춰서 안전하게
    offset = 0
    while True:
        res = (
            sb.table("radio_stations")
            .select("stationuuid, name, url, favicon, country, geo_lat, geo_long")
            .eq("is_hidden", False)
            .eq("is_active", True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        page = res.data or []
        stations.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    clusters: dict[tuple[float, float], dict] = {}
    _add_to_clusters(clusters, stations)

    result = list(clusters.values())
    _markers_cache["data"] = result
    _markers_cache["ts"] = now
    return result


@router.post("/health-check")
async def trigger_health_check(x_user_id: str | None = Header(None)):
    """헬스체크 배치 1회를 수동으로 즉시 실행한다 (관리자 전용, 디버깅용)."""
    _require_admin(x_user_id)
    result = await run_health_check_batch()
    _markers_cache["data"] = None  # 활성 상태가 바뀌었을 수 있으니 마커 캐시도 무효화
    return result
