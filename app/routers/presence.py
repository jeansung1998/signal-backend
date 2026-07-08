from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.database import get_supabase
from app.models import PresenceUpdate

router = APIRouter()


@router.post("/heartbeat")
def heartbeat(payload: PresenceUpdate):
    """
    Called periodically by the client (e.g. every 30s) while the app is open.
    Upserts the user's current city/coords and refreshes last_seen.
    Rows with a stale last_seen are treated as offline (see /presence/cities).
    """
    sb = get_supabase()
    sb.table("presence").upsert(
        {
            "user_id": payload.user_id,
            "city": payload.city,
            "lat": payload.lat,
            "lng": payload.lng,
            "last_seen": "now()",
        }
    ).execute()
    return {"status": "ok"}


@router.get("/cities")
def list_active_cities():
    """
    Returns one row per city currently active (last_seen within the last 2 minutes),
    with an online_count. This is what feeds the globe's marker dots.
    """
    sb = get_supabase()
    res = sb.rpc("active_cities", {}).execute()
    return res.data


@router.get("/cities/{city}/users")
def users_in_city(city: str):
    """
    한 도시에서 활동 중인 사람 목록.

    버그였던 부분: `.gte("last_seen", "now() - interval '2 minutes'")`처럼
    SQL 표현식을 문자열로 그대로 넘기면, PostgREST가 이걸 실제 SQL로
    해석하는 게 아니라 그냥 이상한 텍스트로 취급해서 타임스탬프 비교가
    깨지고 500 에러가 났다. 파이썬에서 실제 기준 시각을 계산해서 값으로
    넘겨야 한다.
    """
    sb = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    res = (
        sb.table("presence")
        .select("user_id, users(nickname, photo_url, intro, greeting_message)")
        .eq("city", city)
        .gte("last_seen", cutoff)
        .execute()
    )
    return res.data
