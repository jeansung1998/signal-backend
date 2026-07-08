"""
TV 채널 API — iptv-org에서 수집한 채널 중 우리 자체 헬스체크를
통과한(is_active=true) 것만 앱에 내려준다.

/ingest, /health-check는 관리자용 수동 트리거 엔드포인트.
스케줄러가 자동으로 돌리지만, 배포 직후 첫 데이터 채우기나
문제 확인용으로 수동 실행할 수 있게 열어둔다.
"""
from fastapi import APIRouter, Header, HTTPException, Query

from app.database import get_supabase
from app.tv_ingest import ingest_iptv_channels
from app.tv_health import run_health_check_batch
from app.routers.admin import ADMIN_USER_ID

router = APIRouter()


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


@router.post("/ingest")
async def trigger_ingest(x_user_id: str | None = Header(None)):
    """iptv-org 데이터를 다시 가져와서 tv_channels에 채워넣는다 (관리자 전용)."""
    _require_admin(x_user_id)
    return await ingest_iptv_channels()


@router.post("/health-check")
async def trigger_health_check(x_user_id: str | None = Header(None)):
    """헬스체크 배치 1회를 수동으로 즉시 실행한다 (관리자 전용, 디버깅용)."""
    _require_admin(x_user_id)
    return await run_health_check_batch()
