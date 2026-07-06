from fastapi import APIRouter, HTTPException

from app.database import get_supabase
from app.models import UserProfile, UserProfileUpdate

router = APIRouter()


@router.get("/{user_id}", response_model=UserProfile)
def get_profile(user_id: str):
    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")
    return res.data


@router.patch("/{user_id}", response_model=UserProfile)
def update_profile(user_id: str, payload: UserProfileUpdate):
    sb = get_supabase()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    res = sb.table("users").update(updates).eq("id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")
    return res.data[0]
