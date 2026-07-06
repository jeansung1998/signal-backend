import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from app.database import get_supabase
from app.models import MatchRequestCreate
from app.connection_manager import manager

router = APIRouter()

MATCH_TIMEOUT_SECONDS = 120
CHAT_TIMEOUT_SECONDS = 120

pending_match_timeouts: dict[str, asyncio.Task] = {}
pending_chat_timeouts: dict[str, asyncio.Task] = {}


@router.post("")
async def create_match_request(payload: MatchRequestCreate):
    sb = get_supabase()

    if not manager.is_online(payload.to_user_id):
        raise HTTPException(status_code=400, detail="상대방이 오프라인 상태입니다")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=MATCH_TIMEOUT_SECONDS)

    res = (
        sb.table("match_requests")
        .insert(
            {
                "from_user_id": payload.from_user_id,
                "to_user_id": payload.to_user_id,
                "status": "pending",
                "expires_at": expires_at.isoformat(),
            }
        )
        .execute()
    )
    request_row = res.data[0]
    request_id = request_row["id"]

    await manager.send_to_user(payload.to_user_id, {
        "type": "match_request",
        "request_id": request_id,
        "from_user_id": payload.from_user_id,
    })

    task = asyncio.create_task(
        _expire_match_request(request_id, payload.from_user_id, payload.to_user_id)
    )
    pending_match_timeouts[request_id] = task

    return request_row


async def _expire_match_request(request_id: str, from_user_id: str, to_user_id: str):
    await asyncio.sleep(MATCH_TIMEOUT_SECONDS)
    sb = get_supabase()

    row = sb.table("match_requests").select("status").eq("id", request_id).execute()
    if not row.data or row.data[0]["status"] != "pending":
        return

    sb.table("match_requests").update({"status": "expired"}).eq("id", request_id).execute()

    for uid in (from_user_id, to_user_id):
        await manager.send_to_user(uid, {"type": "match_expired", "request_id": request_id})

    pending_match_timeouts.pop(request_id, None)


@router.post("/{request_id}/accept")
async def accept_match_request(request_id: str):
    sb = get_supabase()
    req = sb.table("match_requests").select("*").eq("id", request_id).single().execute()
    if not req.data:
        raise HTTPException(status_code=404, detail="Match request not found")

    task = pending_match_timeouts.pop(request_id, None)
    if task:
        task.cancel()

    from_user_id = req.data["from_user_id"]
    to_user_id = req.data["to_user_id"]

    sb.table("match_requests").update({"status": "accepted"}).eq("id", request_id).execute()

    now = datetime.now(timezone.utc)
    room = (
        sb.table("chat_rooms")
        .insert(
            {
                "match_request_id": request_id,
                "user_a": from_user_id,
                "user_b": to_user_id,
                "status": "active",
                "last_activity_at": now.isoformat(),
            }
        )
        .execute()
    )
    room_row = room.data[0]
    room_id = room_row["id"]

    for uid in (from_user_id, to_user_id):
        await manager.send_to_user(uid, {
            "type": "match_accepted",
            "request_id": request_id,
            "room_id": room_id,
        })

    task = asyncio.create_task(_watch_chat_room_timeout(room_id, from_user_id, to_user_id))
    pending_chat_timeouts[room_id] = task

    return room_row


@router.post("/{request_id}/reject")
async def reject_match_request(request_id: str):
    sb = get_supabase()

    task = pending_match_timeouts.pop(request_id, None)
    if task:
        task.cancel()

    req = sb.table("match_requests").select("*").eq("id", request_id).single().execute()
    if req.data:
        await manager.send_to_user(req.data["from_user_id"], {
            "type": "match_rejected",
            "request_id": request_id,
        })

    sb.table("match_requests").update({"status": "rejected"}).eq("id", request_id).execute()
    return {"status": "ok"}


async def _watch_chat_room_timeout(room_id: str, user_a: str, user_b: str):
    await asyncio.sleep(CHAT_TIMEOUT_SECONDS)
    sb = get_supabase()

    row = sb.table("chat_rooms").select("status").eq("id", room_id).execute()
    if not row.data or row.data[0]["status"] != "active":
        return

    sb.table("chat_rooms").update({"status": "closed"}).eq("id", room_id).execute()

    for uid in (user_a, user_b):
        await manager.send_to_user(uid, {"type": "chat_room_closed", "room_id": room_id, "reason": "timeout"})

    pending_chat_timeouts.pop(room_id, None)


def reset_chat_room_timeout(room_id: str, user_a: str, user_b: str):
    old_task = pending_chat_timeouts.pop(room_id, None)
    if old_task:
        old_task.cancel()
    task = asyncio.create_task(_watch_chat_room_timeout(room_id, user_a, user_b))
    pending_chat_timeouts[room_id] = task