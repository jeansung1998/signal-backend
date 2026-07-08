"""
TV 채널 API — iptv-org에서 수집한 채널 중 우리 자체 헬스체크를
통과한(is_active=true) 것만 앱에 내려준다.

/ingest, /health-check는 관리자용 수동 트리거 엔드포인트.
스케줄러가 자동으로 돌리지만, 배포 직후 첫 데이터 채우기나
문제 확인용으로 수동 실행할 수 있게 열어둔다.
"""
import hashlib
import time
from collections import Counter, defaultdict

from fastapi import APIRouter, Header, HTTPException, Query

from app.database import get_supabase
from app.tv_ingest import ingest_iptv_channels
from app.tv_health import run_health_check_batch
from app.routers.admin import ADMIN_USER_ID

router = APIRouter()

_markers_cache: dict = {"data": None, "ts": 0}
MARKERS_CACHE_TTL = 60 * 30  # 30분 캐시 (radio.py의 /markers와 동일한 패턴)

# 채널이 이 개수를 넘는 나라(미국 등)는 대표 좌표 하나에 다 몰아
# 찍지 않고, 여러 지점으로 흩어서 찍는다. 방송국 자체에는 위치
# 정보가 없어서 실제 위치가 아니라 "보기 좋게 흩뿌리는" 용도다.
SCATTER_THRESHOLD = 30
MAX_CHANNELS_PER_CLUSTER = 15  # 한 지점에 채널 15개 넘게 몰리면 지역을 하나 더 만든다
JITTER_DEGREES = 3.0  # 국경 범위를 모르는 나라의 흩뿌리는 폭(대략치, 실제 위치 아님)

# 국경 범위(대략의 bounding box)를 아는 나라는, 좁은 반경으로
# 몰아 흩뿌리지 않고 나라 전역에 고르게 펴서 찍는다.
# (lat_min, lat_max, lng_min, lng_max) — 본토 기준 대략치.
# 필요하면 다른 나라(캐나다/러시아/중국/브라질 등)도 추가하면 된다.
COUNTRY_BBOX = {
    "US": (24.5, 49.5, -125.0, -66.9),
}


def _num_clusters(count: int) -> int:
    if count <= SCATTER_THRESHOLD:
        return 1
    # 올림 나눗셈: 채널 15개당 지역 1개
    return -(-count // MAX_CHANNELS_PER_CLUSTER)


def _cluster_index(channel_id: str, num_clusters: int) -> int:
    """
    채널 id를 해시해서 클러스터 번호를 정한다. 매번 랜덤으로 바뀌면
    사용자가 헷갈리니, 같은 채널은 항상 같은 클러스터/좌표에 찍히게
    결정적(deterministic)으로 계산한다.
    """
    if num_clusters <= 1:
        return 0
    h = int(hashlib.md5(channel_id.encode()).hexdigest(), 16)
    return h % num_clusters


def _scatter_point(base_lat: float, base_lng: float, country_code: str, cluster_idx: int):
    if cluster_idx == 0:
        return base_lat, base_lng

    seed = f"{country_code}:{cluster_idx}"
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)

    bbox = COUNTRY_BBOX.get(country_code)
    if bbox:
        # 국경 범위를 아는 나라는 전역에 고르게 펴서 찍는다.
        lat_min, lat_max, lng_min, lng_max = bbox
        t1 = (h % 10000) / 10000
        t2 = ((h // 10000) % 10000) / 10000
        lat = lat_min + t1 * (lat_max - lat_min)
        lng = lng_min + t2 * (lng_max - lng_min)
        return lat, lng

    # 국경 범위를 모르는 나라는 대표 좌표 주변에 좁게 흩뿌린다.
    dx = ((h % 2000) / 1000 - 1) * JITTER_DEGREES
    dy = (((h // 2000) % 2000) / 1000 - 1) * JITTER_DEGREES
    return base_lat + dx, base_lng + dy


def _fetch_all_active_channels(select: str = "id, country_code") -> list:
    """
    Supabase/PostgREST는 한 번의 select 요청당 기본 최대 1,000개 행까지만
    돌려준다. tv_channels가 1만 개가 넘어서 그냥 .execute()만 하면
    나머지는 통째로 누락된다 (일본처럼 채널이 몇 개 안 되는 나라는
    그 1,000개 안에 우연히 하나도 안 걸리면 마커 자체가 안 생기는
    버그가 있었다). .range()로 전체를 페이지네이션하며 끝까지 훑는다.
    """
    sb = get_supabase()
    rows: list = []
    page_size = 1000
    start = 0
    while True:
        page = (
            sb.table("tv_channels")
            .select(select)
            .eq("is_active", True)
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = page.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def _require_admin(x_user_id: str | None):
    if x_user_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Admin access only")


@router.get("/channels")
def list_channels(
    country_code: str | None = Query(None, description="예: KR, JP"),
    category: str | None = Query(None, description="예: news, general"),
    cluster: int | None = Query(None, description="나라를 여러 지점으로 흩뿌렸을 때의 지점 번호"),
    limit: int = 200,
):
    sb = get_supabase()
    query = sb.table("tv_channels").select("*").eq("is_active", True)
    if country_code:
        query = query.eq("country_code", country_code.upper())
    if category:
        query = query.eq("category", category)
    res = query.limit(2000 if cluster is not None else limit).execute()
    rows = res.data or []

    if cluster is not None and country_code:
        # 마커와 똑같은 규칙으로 클러스터 개수를 계산해서, 그 클러스터에
        # 속하는 채널만 걸러낸다. (전체 개수는 이 나라의 활성 채널
        # 총합 기준 — 마커 생성 시 쓰는 것과 같은 값이어야 클러스터
        # 경계가 어긋나지 않는다)
        num_clusters = _num_clusters(len(rows))
        rows = [r for r in rows if _cluster_index(r["id"], num_clusters) == cluster]
        rows = rows[:limit]
    else:
        rows = rows[:limit]

    return rows


@router.get("/markers")
def tv_markers():
    """
    지구본에 찍을 TV 마커 목록.

    iptv-org 채널 데이터에는 라디오와 달리 개별 방송국 위경도가 없고
    국가 코드만 있어서, 라디오처럼 진짜 좌표 클러스터링을 할 수가
    없다. 대신 국가별로 활성 채널 개수를 세고, `places` 테이블에서
    그 나라의 대표 좌표를 가져와 붙인다.

    채널이 SCATTER_THRESHOLD를 넘는 나라(미국 등)는 대표 좌표 한
    점에 다 몰아 찍으면 지구본에서 뭉개져 보이므로, 최대
    MAX_CLUSTERS_PER_COUNTRY개 지점으로 나눠 흩뿌린다. 실제 방송국
    위치가 아니라 순전히 보기 좋게 나누는 용도이며, 채널 id 기준으로
    결정적으로 계산해서 새로고침해도 항상 같은 자리에 찍힌다.

    매번 채널 전체 + places 전체를 다시 계산하면 지구본 로드할 때마다
    부담이 커서, radio.py의 /markers와 같은 패턴으로 30분 캐시한다.
    """
    now = time.time()
    if _markers_cache["data"] is not None and (now - _markers_cache["ts"]) < MARKERS_CACHE_TTL:
        return _markers_cache["data"]

    channels = _fetch_all_active_channels("id, country_code")

    by_country: dict = defaultdict(list)
    for c in channels:
        cc = c.get("country_code")
        if cc:
            by_country[cc].append(c["id"])

    sb = get_supabase()
    places_res = sb.table("places").select("id, country_code, country, lat, lng").order("id").execute()
    places_by_country: dict = {}
    for p in (places_res.data or []):
        cc = p.get("country_code")
        if cc and cc not in places_by_country:
            # 한 나라에 도시가 여러 개 있으면(한국/일본/호주 등 라디오
            # 지역 확충 때 추가된 것들) id가 가장 빠른(=보통 수도) 것만
            # 쓴다. 안 그러면 오키나와처럼 변두리 도시가 뽑힐 수 있다.
            places_by_country[cc] = p

    markers = []
    for cc, channel_ids in by_country.items():
        place = places_by_country.get(cc)
        if not place:
            continue

        count = len(channel_ids)
        num_clusters = _num_clusters(count)

        if num_clusters == 1:
            markers.append({
                "country_code": cc,
                "country": place["country"],
                "lat": place["lat"],
                "lng": place["lng"],
                "count": count,
                "cluster": 0,
            })
            continue

        cluster_counts = Counter(_cluster_index(cid, num_clusters) for cid in channel_ids)
        for cluster_idx, cluster_count in cluster_counts.items():
            lat, lng = _scatter_point(place["lat"], place["lng"], cc, cluster_idx)
            markers.append({
                "country_code": cc,
                "country": place["country"],
                "lat": lat,
                "lng": lng,
                "count": cluster_count,
                "cluster": cluster_idx,
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
