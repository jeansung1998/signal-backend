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
    sb = get_supabase()
    res = (
        sb.table("presence")
        .select("user_id, users(nickname, photo_url, intro, greeting_message)")
        .eq("city", city)
        .gte("last_seen", "now() - interval '2 minutes'")
        .execute()
    )
    return res.data
