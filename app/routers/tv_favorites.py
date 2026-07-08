from fastapi import APIRouter, Header, HTTPException

from app.database import get_supabase

router = APIRouter()

MAX_FAVORITES = 20


@router.get("/tv/favorites")
def list_favorites(x_user_id: str = Header(...)):
    sb = get_supabase()
    res = (
        sb.table("tv_favorites")
        .select("channel_id, created_at")
        .eq("user_id", x_user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data


@router.post("/tv/favorites/{channel_id}")
def add_favorite(channel_id: str, x_user_id: str = Header(...)):
    sb = get_supabase()

    count_res = (
        sb.table("tv_favorites")
        .select("channel_id", count="exact")
        .eq("user_id", x_user_id)
        .execute()
    )
    if count_res.count is not None and count_res.count >= MAX_FAVORITES:
        raise HTTPException(status_code=400, detail=f"즐겨찾기는 최대 {MAX_FAVORITES}개까지 가능합니다.")

    sb.table("tv_favorites").upsert({
        "user_id": x_user_id,
        "channel_id": channel_id,
    }).execute()
    return {"status": "ok"}


@router.delete("/tv/favorites/{channel_id}")
def remove_favorite(channel_id: str, x_user_id: str = Header(...)):
    sb = get_supabase()
    sb.table("tv_favorites").delete().eq("user_id", x_user_id).eq("channel_id", channel_id).execute()
    return {"status": "ok"}