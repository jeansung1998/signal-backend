from datetime import datetime, timezone
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database import get_supabase
from app.connection_manager import manager
from app.routers.match import reset_chat_room_timeout
from app.translation import (
    resolve_target_language,
    translate_text,
    set_user_language_override,
    clear_user_language_override,
)

router = APIRouter()


@router.websocket("/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "chat_message":
                await _handle_chat_message(user_id, data)
            elif msg_type == "set_language":
                await _handle_set_language(user_id, data)
            elif msg_type == "leave_chat_room":
                await _handle_leave_chat_room(user_id, data)
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        sb = get_supabase()
        rooms_res = (
            sb.table("chat_rooms")
            .select("*")
            .or_(f"user_a.eq.{user_id},user_b.eq.{user_id}")
            .eq("status", "active")
            .execute()
        )
        for room in rooms_res.data:
            other_id = room["user_b"] if room["user_a"] == user_id else room["user_a"]
            await manager.send_to_user(other_id, {"type": "user_left", "room_id": room["id"]})


async def _handle_set_language(user_id: str, data: dict):
    """
    Lets a user override the language their incoming messages get
    translated into (the "choose recipient's language" button).
    Send {"type": "set_language", "lang": "ja"} to set it, or
    {"type": "set_language", "lang": null} to go back to the
    country_code-based default.
    """
    lang = data.get("lang")
    if lang:
        set_user_language_override(user_id, lang)
    else:
        clear_user_language_override(user_id)
    await manager.send_to_user(user_id, {"type": "language_set", "lang": lang})


async def _handle_leave_chat_room(user_id: str, data: dict):
    """
    Fired when a user explicitly clicks "채팅방 나가기" and sends
    {"type": "leave_chat_room", "room_id": "..."} over the socket
    (their socket connection itself stays open, since it's one
    connection per user, not per room). Notify the other participant
    so they see "상대방이 퇴장하셨습니다" in their chat window.
    """
    sb = get_supabase()
    room_id = data.get("room_id")
    if not room_id:
        return

    room_row = sb.table("chat_rooms").select("*").eq("id", room_id).execute()
    if not room_row.data:
        return
    room = room_row.data[0]

    user_a, user_b = room["user_a"], room["user_b"]
    if user_id not in (user_a, user_b):
        return

    other_id = user_b if user_id == user_a else user_a
    await manager.send_to_user(other_id, {"type": "user_left", "room_id": room_id})


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

    # We keep the original text in the DB (audit trail / admin view).
    # Translation happens per-recipient at send time since two people
    # in the same room can each want a different target language.
    msg_result = sb.table("messages").insert({
        "room_id": room_id,
        "sender_id": sender_id,
        "content": content,
    }).execute()
    saved_message = msg_result.data[0]

    sb.table("chat_rooms").update({"last_activity_at": now.isoformat()}).eq("id", room_id).execute()
    reset_chat_room_timeout(room_id, user_a, user_b)

    receiver_id = user_b if sender_id == user_a else user_a

    receiver_row = sb.table("users").select("country_code").eq("id", receiver_id).single().execute()
    receiver_country = receiver_row.data.get("country_code") if receiver_row.data else None
    target_lang = resolve_target_language(receiver_id, receiver_country)
    translated_content = await translate_text(content, target_lang)

    # Sender sees their own message exactly as they typed it.
    sender_payload = {
        "type": "chat_message",
        "room_id": room_id,
        "message_id": saved_message["id"],
        "sender_id": sender_id,
        "content": content,
        "created_at": saved_message["created_at"],
    }
    # Receiver only ever sees the translated version — the original
    # is not sent to them, per the "auto-translate, hide original" design.
    receiver_payload = {
        **sender_payload,
        "content": translated_content,
        "translated": translated_content != content,
    }

    await manager.send_to_user(sender_id, sender_payload)
    await manager.send_to_user(receiver_id, receiver_payload)