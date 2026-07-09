"""
Admin dashboard API.

Everything here is read-only reporting for the admin dashboard:
member stats, recent signups, member search, per-country breakdown,
and DB storage usage against Supabase's free-tier limits (with a
warning threshold so we notice before hitting the ceiling and have
time to plan an upgrade or cleanup).

Access is restricted to Ryker's account only (see ADMIN_USER_ID) via
an `X-User-Id` header the frontend must send with every admin
request. This is a lightweight guard, not full auth — fine for a
single-admin dashboard, but worth revisiting (real JWT-based admin
auth) if more admins are ever added.
"""
import os

from fastapi import APIRouter, Header, HTTPException, Query

from app.database import get_supabase
from app.radio_health import probe_station
from app.tv_health import probe_channel

router = APIRouter()

ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "675e7352-fbcc-4ec2-b2fd-96e33bbd1e0c")

# Supabase free-tier limits (check Supabase's pricing page if these
# ever seem stale — noted here as of mid-2026). We warn once usage
# crosses WARNING_THRESHOLD of the limit, so there's time to upgrade
# or clean up before actually hitting the ceiling.
DB_SIZE_LIMIT_BYTES = 500 * 1024 * 1024        # 500 MB database (free tier)
STORAGE_LIMIT_BYTES = 1 * 1024 * 1024 * 1024   # 1 GB file storage (free tier)
WARNING_THRESHOLD = 0.8


def _require_admin(x_user_id: str | None):
    if x_user_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Admin access only")


@router.get("/overview")
def stats_overview(x_user_id: str | None = Header(None)):
    """전체 회원 수 + 오늘 신규가입자 수."""
    _require_admin(x_user_id)
    sb = get_supabase()
    total = sb.table("admin_total_users").select("*").execute()
    today = sb.table("admin_new_signups_today").select("*").execute()
    return {
        "total_users": total.data[0]["count"] if total.data else 0,
        "new_signups_today": today.data[0]["count"] if today.data else 0,
    }


@router.get("/users/by-country")
def users_by_country(x_user_id: str | None = Header(None)):
    """국가별 회원 분포."""
    _require_admin(x_user_id)
    sb = get_supabase()
    res = sb.table("admin_users_by_country").select("*").execute()
    return res.data


@router.get("/users/recent")
def recent_signups(limit: int = 100, x_user_id: str | None = Header(None)):
    """최근 가입자 목록 (기본 최근 100명)."""
    _require_admin(x_user_id)
    sb = get_supabase()
    res = sb.table("admin_recent_signups").select("*").limit(limit).execute()
    return res.data


@router.get("/users")
def list_users(
    x_user_id: str | None = Header(None),
    search: str | None = Query(None, description="닉네임으로 검색"),
    country_code: str | None = Query(None, description="예: KR, US"),
    limit: int = 50,
    offset: int = 0,
):
    """
    전체 회원 목록 — 검색/국가 필터 + 페이지네이션 지원.
    admin_recent_signups(최근 100명 고정 뷰)와 달리 회원관리 화면에서
    전체 목록을 넘기며 보고 검색할 때 쓰는 용도.
    """
    _require_admin(x_user_id)
    sb = get_supabase()
    query = sb.table("users").select(
        "id, nickname, country_code, created_at, is_traveler, travel_city",
        count="exact",
    )
    if search:
        query = query.ilike("nickname", f"%{search}%")
    if country_code:
        query = query.eq("country_code", country_code.upper())

    res = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return {"total": res.count, "users": res.data}


@router.get("/storage")
def storage_usage(x_user_id: str | None = Header(None)):
    """
    테이블별 DB 용량 + 무료 티어 한도(500MB) 대비 경고 플래그.
    80% 넘으면 warning=true와 함께 안내 메시지를 같이 내려준다.

    참고: Storage(파일) 버킷 용량은 Postgres 쿼리로 못 가져와서
    지금은 DB 용량만 체크한다. 나중에 필요하면 Supabase Storage API로
    버킷별 용량도 추가할 수 있다.
    """
    _require_admin(x_user_id)
    sb = get_supabase()
    res = sb.table("admin_table_sizes").select("*").execute()
    tables = res.data or []

    total_bytes = sum(t.get("size_bytes", 0) for t in tables)
    usage_ratio = (total_bytes / DB_SIZE_LIMIT_BYTES) if DB_SIZE_LIMIT_BYTES else 0
    is_warning = usage_ratio >= WARNING_THRESHOLD

    return {
        "tables": tables,
        "total_bytes": total_bytes,
        "limit_bytes": DB_SIZE_LIMIT_BYTES,
        "usage_ratio": round(usage_ratio, 4),
        "warning": is_warning,
        "warning_message": (
            f"DB 사용량이 무료 티어 한도의 {round(usage_ratio * 100)}%에 도달했습니다. "
            "Pro 플랜 업그레이드나 데이터 정리를 고려하세요."
            if is_warning else None
        ),
    }
@router.get("/quality")
def quality_metrics(x_user_id: str | None = Header(None)):
    """
    서비스 품질 지표: 활성 채널 비율, 매칭 성공률.
    """
    _require_admin(x_user_id)
    sb = get_supabase()

    total_channels = sb.table("tv_channels").select("id", count="exact").execute()
    active_channels = (
        sb.table("tv_channels").select("id", count="exact").eq("is_active", True).execute()
    )
    total_ch = total_channels.count or 0
    active_ch = active_channels.count or 0
    active_ratio = (active_ch / total_ch) if total_ch else 0

    total_matches = sb.table("match_requests").select("id", count="exact").execute()
    accepted_matches = (
        sb.table("match_requests").select("id", count="exact").eq("status", "accepted").execute()
    )
    total_m = total_matches.count or 0
    accepted_m = accepted_matches.count or 0
    success_ratio = (accepted_m / total_m) if total_m else 0

    total_stations = sb.table("radio_stations").select("stationuuid", count="exact").execute()
    active_stations = (
        sb.table("radio_stations").select("stationuuid", count="exact").eq("is_active", True).execute()
    )
    total_st = total_stations.count or 0
    active_st = active_stations.count or 0
    st_active_ratio = (active_st / total_st) if total_st else 0

    return {
        "tv_channels": {
            "total": total_ch,
            "active": active_ch,
            "active_ratio": round(active_ratio, 4),
        },
        "radio_stations": {
            "total": total_st,
            "active": active_st,
            "active_ratio": round(st_active_ratio, 4),
        },
        "match_requests": {
            "total": total_m,
            "accepted": accepted_m,
            "success_ratio": round(success_ratio, 4),
        },
    }


# ---------------------------------------------------------------
# 죽은 방송국/채널 관리 — 헬스체크가 is_active=false로 내린 것들을
# 어드민 화면에서 확인하고, 확실히 죽었으면 영구 제외(is_hidden)
# 시키거나, 다시 살아난 것 같으면 복구시킬 수 있게 한다.
# ---------------------------------------------------------------
_STATION_TABLES = {
    "radio": {"table": "radio_stations", "id_field": "stationuuid", "country_field": "country"},
    "tv": {"table": "tv_channels", "id_field": "id", "country_field": "country_code"},
}


@router.get("/stations/dead")
def list_dead_stations(
    x_user_id: str | None = Header(None),
    type: str = Query(..., description="radio 또는 tv"),
    limit: int = 50,
    offset: int = 0,
):
    """
    헬스체크에서 죽은 것으로 판정된(is_active=false) 방송국/채널 목록.
    아직 영구 제외(is_hidden) 처리 안 된 것들만 보여준다 — 이미
    영구 제외한 건 검토가 끝난 거니 목록에서 뺀다.
    consecutive_fail_count가 높은 순(오래 죽어있는 순)으로 정렬.
    """
    _require_admin(x_user_id)
    if type not in _STATION_TABLES:
        raise HTTPException(status_code=400, detail="type은 'radio' 또는 'tv'여야 합니다")

    cfg = _STATION_TABLES[type]
    sb = get_supabase()
    country_field = cfg["country_field"]
    id_field = cfg["id_field"]
    select_fields = f"{id_field}, name, url, {country_field}, consecutive_fail_count, last_checked_at, last_ok_at"
    res = (
        sb.table(cfg["table"])
        .select(select_fields, count="exact")
        .eq("is_active", False)
        .eq("is_hidden", False)
        .order("consecutive_fail_count", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {"total": res.count, "items": res.data or []}


@router.post("/stations/hide")
def hide_station(
    body: dict,
    x_user_id: str | None = Header(None),
):
    """
    확인 후 확실히 죽은 방송국/채널을 영구 제외시킨다 (is_hidden=true).
    이후 헬스체크 대상에서도 빠지고, 앱에도 다시 노출되지 않는다.
    body: {"type": "radio"|"tv", "id": "..."}
    """
    _require_admin(x_user_id)
    type_ = body.get("type")
    item_id = body.get("id")
    if type_ not in _STATION_TABLES or not item_id:
        raise HTTPException(status_code=400, detail="type과 id가 필요합니다")

    cfg = _STATION_TABLES[type_]
    sb = get_supabase()
    sb.table(cfg["table"]).update({"is_hidden": True}).eq(cfg["id_field"], item_id).execute()
    return {"ok": True}


@router.post("/stations/restore")
def restore_station(
    body: dict,
    x_user_id: str | None = Header(None),
):
    """
    다시 살아난 것 같은 방송국/채널을 원상복구한다 — 실패 카운트를
    리셋하고 활성 처리해서 다음 헬스체크 때부터 정상 취급되게 한다.
    body: {"type": "radio"|"tv", "id": "..."}
    """
    _require_admin(x_user_id)
    type_ = body.get("type")
    item_id = body.get("id")
    if type_ not in _STATION_TABLES or not item_id:
        raise HTTPException(status_code=400, detail="type과 id가 필요합니다")

    cfg = _STATION_TABLES[type_]
    sb = get_supabase()
    sb.table(cfg["table"]).update({
        "is_active": True,
        "is_hidden": False,
        "consecutive_fail_count": 0,
    }).eq(cfg["id_field"], item_id).execute()
    return {"ok": True}


# ---------------------------------------------------------------
# Inspection 대시보드 — 방송국/채널을 국가별로 훑어보고, 상태별로
# 필터링하고, 추이 그래프를 보고, 여러 개를 한 번에 정리하거나
# 새 방송국/채널을 직접 등록할 수 있게 하는 종합 관리 화면.
# ---------------------------------------------------------------

@router.get("/stations/overview")
def stations_overview(
    x_user_id: str | None = Header(None),
    type: str = Query(..., description="radio 또는 tv"),
):
    """
    전체 개수 / 활성 / 비활성 개수 + 국가별 분포(활성/비활성 나눠서).
    Inspection 대시보드 첫 화면에서 쓰는 요약 통계.
    """
    _require_admin(x_user_id)
    if type not in _STATION_TABLES:
        raise HTTPException(status_code=400, detail="type은 'radio' 또는 'tv'여야 합니다")
    cfg = _STATION_TABLES[type]
    sb = get_supabase()

    total = sb.table(cfg["table"]).select(cfg["id_field"], count="exact").eq("is_hidden", False).execute()
    active = (
        sb.table(cfg["table"]).select(cfg["id_field"], count="exact")
        .eq("is_hidden", False).eq("is_active", True).execute()
    )
    total_n = total.count or 0
    active_n = active.count or 0
    dead_n = total_n - active_n

    # 국가별 분포는 행이 많아 파이썬에서 직접 집계 (country, is_active만 가져옴)
    country_field = cfg["country_field"]
    country_rows: list = []
    page_size = 900
    offset = 0
    while True:
        res = (
            sb.table(cfg["table"]).select(f"{country_field}, is_active")
            .eq("is_hidden", False)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        page = res.data or []
        country_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    by_country: dict[str, dict] = {}
    for r in country_rows:
        c = r.get(country_field) or "미상"
        entry = by_country.setdefault(c, {"country": c, "total": 0, "active": 0, "dead": 0})
        entry["total"] += 1
        if r.get("is_active"):
            entry["active"] += 1
        else:
            entry["dead"] += 1

    countries = sorted(by_country.values(), key=lambda x: x["total"], reverse=True)

    return {
        "total": total_n,
        "active": active_n,
        "dead": dead_n,
        "countries": countries,
    }


@router.get("/stations/list")
def stations_list(
    x_user_id: str | None = Header(None),
    type: str = Query(..., description="radio 또는 tv"),
    country: str | None = Query(None, description="국가 코드/이름으로 필터"),
    status: str | None = Query(None, description="'active' 또는 'dead' (생략하면 전체)"),
    search: str | None = Query(None, description="이름으로 검색"),
    limit: int = 50,
    offset: int = 0,
):
    """
    국가/상태/검색어로 필터링한 방송국/채널 목록. Inspection 대시보드에서
    국가 카드를 클릭했을 때, 또는 검색할 때 쓰는 용도.
    """
    _require_admin(x_user_id)
    if type not in _STATION_TABLES:
        raise HTTPException(status_code=400, detail="type은 'radio' 또는 'tv'여야 합니다")
    cfg = _STATION_TABLES[type]
    sb = get_supabase()
    country_field = cfg["country_field"]
    id_field = cfg["id_field"]

    select_fields = (
        f"{id_field}, name, url, {country_field}, is_active, consecutive_fail_count, last_checked_at, last_ok_at"
    )
    query = sb.table(cfg["table"]).select(select_fields, count="exact").eq("is_hidden", False)
    if country:
        query = query.eq(country_field, country)
    if status == "active":
        query = query.eq("is_active", True)
    elif status == "dead":
        query = query.eq("is_active", False)
    if search:
        query = query.ilike("name", f"%{search}%")

    res = query.order("name").range(offset, offset + limit - 1).execute()
    return {"total": res.count, "items": res.data or []}


@router.get("/stations/history")
def stations_history(
    x_user_id: str | None = Header(None),
    type: str = Query(..., description="radio 또는 tv"),
    limit: int = 50,
):
    """
    헬스체크 배치 기록 추이 (수신/송출 그래프용) — 최근 실행분부터
    오래된 순으로 최대 limit개. 프론트에서 시간순으로 뒤집어서
    그래프 그리면 된다.
    """
    _require_admin(x_user_id)
    if type not in _STATION_TABLES:
        raise HTTPException(status_code=400, detail="type은 'radio' 또는 'tv'여야 합니다")
    sb = get_supabase()
    res = (
        sb.table("station_health_log")
        .select("checked, alive, deactivated, created_at")
        .eq("type", type)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"items": list(reversed(res.data or []))}


@router.post("/stations/bulk-hide")
def bulk_hide_stations(
    body: dict,
    x_user_id: str | None = Header(None),
):
    """
    여러 개를 한 번에 영구 제외 (페이지 전체 삭제용).
    body: {"type": "radio"|"tv", "ids": ["...", "..."]}
    """
    _require_admin(x_user_id)
    type_ = body.get("type")
    ids = body.get("ids")
    if type_ not in _STATION_TABLES or not ids or not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="type과 ids(배열)가 필요합니다")

    cfg = _STATION_TABLES[type_]
    sb = get_supabase()
    sb.table(cfg["table"]).update({"is_hidden": True}).in_(cfg["id_field"], ids).execute()
    return {"ok": True, "hidden_count": len(ids)}


@router.post("/stations/add")
def add_station(
    body: dict,
    x_user_id: str | None = Header(None),
):
    """
    새 방송국/채널을 수동으로 등록한다.
    body(radio): {"type": "radio", "name": "...", "url": "...", "country": "...",
                   "countrycode": "KR", "geo_lat": 37.5, "geo_long": 127.0}
    body(tv):    {"type": "tv", "name": "...", "url": "...", "country": "..."}
    """
    _require_admin(x_user_id)
    type_ = body.get("type")
    name = body.get("name")
    url = body.get("url")
    if type_ not in _STATION_TABLES or not name or not url:
        raise HTTPException(status_code=400, detail="type, name, url이 필요합니다")

    sb = get_supabase()
    if type_ == "radio":
        import uuid
        row = {
            "stationuuid": str(uuid.uuid4()),
            "name": name,
            "url": url,
            "country": body.get("country", ""),
            "countrycode": body.get("countrycode", ""),
            "geo_lat": body.get("geo_lat"),
            "geo_long": body.get("geo_long"),
            "is_hidden": False,
            "is_active": True,
            "is_synthetic_geo": body.get("geo_lat") is None,
        }
        sb.table("radio_stations").insert(row).execute()
    else:
        import re
        import uuid
        slug = re.sub(r"[^a-zA-Z0-9]+", "", name)[:20] or "channel"
        row = {
            "id": f"manual-{slug}-{uuid.uuid4().hex[:8]}",
            "name": name,
            "url": url,
            "country_code": body.get("country", ""),
            "is_hidden": False,
            "is_active": True,
        }
        sb.table("tv_channels").insert(row).execute()

    return {"ok": True}


@router.post("/stations/test")
async def test_station_now(
    body: dict,
    x_user_id: str | None = Header(None),
):
    """
    방송국/채널 하나를 지금 이 순간 실제로 검사한다 (전선 테스터처럼
    "지금 흐르는지" 즉석 확인). 자동 헬스체크 결과를 기다리지 않고
    Inspection 화면에서 바로 눌러서 확인할 때 쓴다. DB는 건드리지
    않고 결과만 돌려준다 — 필요하면 확인 후 직접 hide/restore로
    처리하면 된다.
    body: {"type": "radio"|"tv", "url": "..."}
    """
    _require_admin(x_user_id)
    type_ = body.get("type")
    url = body.get("url")
    if type_ not in _STATION_TABLES or not url:
        raise HTTPException(status_code=400, detail="type과 url이 필요합니다")

    if type_ == "radio":
        result = await probe_station(url)
    else:
        result = await probe_channel(url)
    return result