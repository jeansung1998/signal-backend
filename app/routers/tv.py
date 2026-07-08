"""
TV 채널 API — iptv-org에서 수집한 채널 중 우리 자체 헬스체크를
통과한(is_active=true) 것만 앱에 내려준다.

/ingest, /health-check는 관리자용 수동 트리거 엔드포인트.
스케줄러가 자동으로 돌리지만, 배포 직후 첫 데이터 채우기나
문제 확인용으로 수동 실행할 수 있게 열어둔다.
"""
import time
from collections import Counter

from fastapi import APIRouter, Header, HTTPException, Query

from app.database import get_supabase
from app.tv_ingest import ingest_iptv_channels
from app.tv_health import run_health_check_batch
from app.routers.admin import ADMIN_USER_ID

router = APIRouter()

_markers_cache: dict = {"data": None, "ts": 0}
MARKERS_CACHE_TTL = 60 * 30  # 30분 캐시 (radio.py의 /markers와 동일한 패턴)


def _require_admin(x_user_id: str | None):
    if x_user_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Admin access only")


@router.get("/channels")
def list_channels(
    country_code: str | None = Query(None, description="예: KR, JP"),
    category: str | None = Query(None, description="예: news, general"),
    limit: int = 200,
):
    sb = get_supabase()
    query = sb.table("tv_channels").select("*").eq("is_active", True)
    if country_code:
        query = query.eq("country_code", country_code.upper())
    if category:
        query = query.eq("category", category)
    res = query.limit(limit).execute()
    return res.data


@router.get("/markers")
def tv_markers():
    """
    지구본에 찍을 'TV 국가' 마커 목록.

    iptv-org 채널 데이터에는 라디오와 달리 개별 방송국 위경도가 없고
    국가 코드만 있어서, 라디오처럼 좌표 클러스터링을 할 수가 없다.
    대신 국가별로 활성 채널 개수를 세고, 이미 갖고 있는 `places`
    테이블에서 그 나라의 대표 좌표(수도/주요 도시 기준)를 가져와
    붙여서 국가 단위 마커로 반환한다.

    매번 채널 전체 + places 전체를 다시 계산하면 지구본 로드할 때마다
    부담이 커서, radio.py의 /markers와 같은 패턴으로 30분 캐시한다.
    (헬스체크가 1시간마다 도니 30분 캐시면 충분히 최신 상태 유지됨)
    """
    now = time.time()
    if _markers_cache["data"] is not None and (now - _markers_cache["ts"]) < MARKERS_CACHE_TTL:
        return _markers_cache["data"]

    sb = get_supabase()
    channels_res = sb.table("tv_channels").select("country_code").eq("is_active", True).execute()
    channels = channels_res.data or []
    counts = Counter(c["country_code"] for c in channels if c.get("country_code"))

    places_res = sb.table("places").select("country_code, country, lat, lng").execute()
    places_by_country = {p["country_code"]: p for p in (places_res.data or [])}

    markers = []
    for cc, count in counts.items():
        place = places_by_country.get(cc)
        if not place:
            continue
        markers.append({
            "country_code": cc,
            "country": place["country"],
            "lat": place["lat"],
            "lng": place["lng"],
            "count": count,
        })

    _markers_cache["data"] = markers
    _markers_cache["ts"] = now
    return markers


@router.post("/ingest")
async def trigger_ingest(x_user_id: str | None = Header(None)):
    """iptv-org 데이터를 다시 가져와서 tv_channels에 채워넣는다 (관리자 전용)."""
    _require_admin(x_user_id)
    result = await ingest_iptv_channels()
    _markers_cache["data"] = None  # 새로 수집했으니 마커 캐시도 무효화
    return result


@router.post("/health-check")
async def trigger_health_check(x_user_id: str | None = Header(None)):
    """헬스체크 배치 1회를 수동으로 즉시 실행한다 (관리자 전용, 디버깅용)."""
    _require_admin(x_user_id)
    result = await run_health_check_batch()
    _markers_cache["data"] = None  # 활성 상태가 바뀌었을 수 있으니 마커 캐시도 무효화
    return result
