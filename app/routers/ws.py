from datetime import datetime, timezone
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database import get_supabase
from app.connection_manager import manager
from app.routers.match import reset_chat_room_timeout

router = APIRouter()


@router.websocket("/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "chat_message":
                await _handle_chat_message(user_id, data)
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)


async def _handle_chat_message(sender_id: str, data: dict):
    sb = get_supabase()
    room_id = data.get("room_id")
    content = data.get("content")
    if not room_id or not content:
        return

    room_row = sb.table("chat_rooms").select("*").eq("id", room_id).execute()
    if not room_row.data:
        return
    room = room_row.data[0]

    if room.get("status") != "active":
        return

    user_a, user_b = room["user_a"], room["user_b"]
    if sender_id not in (user_a, user_b):
        return

    now = datetime.now(timezone.utc)

    msg_result = sb.table("messages").insert({
        "room_id": room_id,
        "sender_id": sender_id,
        "content": content,
    }).execute()
    saved_message = msg_result.data[0]

    sb.table("chat_rooms").update({"last_activity_at": now.isoformat()}).eq("id", room_id).execute()
    reset_chat_room_timeout(room_id, user_a, user_b)

    receiver_id = user_b if sender_id == user_a else user_a
    payload = {
        "type": "chat_message",
        "room_id": room_id,
        "message_id": saved_message["id"],
        "sender_id": sender_id,
        "content": content,
        "created_at": saved_message["created_at"],
    }
    await manager.send_to_user(sender_id, payload)
    await manager.send_to_user(receiver_id, payload)