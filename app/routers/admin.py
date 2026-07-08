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

    return {
        "tv_channels": {
            "total": total_ch,
            "active": active_ch,
            "active_ratio": round(active_ratio, 4),
        },
        "match_requests": {
            "total": total_m,
            "accepted": accepted_m,
            "success_ratio": round(success_ratio, 4),
        },
    }