"""
`radio_stations`의 스트림 URL들을 주기적으로 점검해서 죽은 방송국을
걸러낸다. tv_health.py와 동일한 패턴 — 배치로 나눠서 점검하고,
연속 FAIL_THRESHOLD번 실패해야 is_active=false로 내린다.

단순히 "연결이 되는가"(상태 코드만 확인)로는 안 걸러지는 경우가 많다
— 서버는 응답하는데 실제로는 오디오가 안 나오거나(무음/빈 스트림),
HTML 에러 페이지를 200으로 돌려주는 등. 그래서 실제로 오디오 바이트가
몇 KB라도 흘러나오는지, Content-Type이 진짜 오디오 계열인지까지
확인한다 — "전선에 손대서 전류가 흐르는지 확인"하는 것과 같은 원리.
"""
import asyncio
import time
from datetime import datetime, timezone

import httpx

from app.database import get_supabase

BATCH_SIZE = 300
CONCURRENCY = 20
TIMEOUT_SECONDS = 8
FAIL_THRESHOLD = 3  # 연속 3번 실패하면 비활성화

MIN_BYTES = 2048  # 이 이상 실제로 받아야 "진짜 흐른다"고 판단 (2KB)
AUDIO_CONTENT_TYPE_HINTS = ("audio", "mpeg", "ogg", "aac", "mp3", "octet-stream")


async def probe_station(url: str) -> dict:
    """
    방송국 하나를 실제로 검사해서 상세 결과를 돌려준다 (진단용).
    자동 배치 헬스체크와 어드민의 '지금 확인' 버튼 둘 다 이 함수를 쓴다.
    """
    started = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET", url, follow_redirects=True, timeout=TIMEOUT_SECONDS
            ) as res:
                if res.status_code >= 400:
                    return {
                        "ok": False,
                        "reason": f"status_{res.status_code}",
                        "status_code": res.status_code,
                        "bytes_received": 0,
                        "content_type": res.headers.get("content-type", ""),
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }

                content_type = res.headers.get("content-type", "").lower()
                bytes_received = 0
                async for chunk in res.aiter_bytes():
                    bytes_received += len(chunk)
                    if bytes_received >= MIN_BYTES:
                        break

                is_audio_type = any(h in content_type for h in AUDIO_CONTENT_TYPE_HINTS) or content_type == ""
                ok = bytes_received >= MIN_BYTES and is_audio_type

                reason = "ok"
                if bytes_received < MIN_BYTES:
                    reason = "no_data"  # 연결은 됐지만 실제 데이터가 안 흐름 (무음/빈 스트림)
                elif not is_audio_type:
                    reason = "not_audio"  # 오디오가 아닌 걸 돌려줌 (HTML 에러 페이지 등)

                return {
                    "ok": ok,
                    "reason": reason,
                    "status_code": res.status_code,
                    "bytes_received": bytes_received,
                    "content_type": content_type,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
    except httpx.TimeoutException:
        return {
            "ok": False, "reason": "timeout", "status_code": None,
            "bytes_received": 0, "content_type": "",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    except httpx.HTTPError as e:
        return {
            "ok": False, "reason": f"connection_error", "status_code": None,
            "bytes_received": 0, "content_type": "",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }


async def _check_one(url: str) -> bool:
    result = await probe_station(url)
    return result["ok"]


async def run_health_check_batch() -> dict:
    sb = get_supabase()
    res = (
        sb.table("radio_stations")
        .select("stationuuid, url, consecutive_fail_count")
        .eq("is_hidden", False)
        .order("last_checked_at", desc=False, nullsfirst=True)
        .limit(BATCH_SIZE)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return {"checked": 0}

    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, bool] = {}

    async def worker(row):
        async with sem:
            results[row["stationuuid"]] = await _check_one(row["url"])

    await asyncio.gather(*(worker(r) for r in rows))

    now = datetime.now(timezone.utc).isoformat()
    alive_count = 0
    deactivated = 0
    for row in rows:
        ok = results.get(row["stationuuid"], False)
        if ok:
            alive_count += 1
            sb.table("radio_stations").update({
                "is_active": True,
                "consecutive_fail_count": 0,
                "last_checked_at": now,
                "last_ok_at": now,
            }).eq("stationuuid", row["stationuuid"]).execute()
        else:
            fails = row["consecutive_fail_count"] + 1
            update = {"consecutive_fail_count": fails, "last_checked_at": now}
            if fails >= FAIL_THRESHOLD:
                update["is_active"] = False
                deactivated += 1
            sb.table("radio_stations").update(update).eq("stationuuid", row["stationuuid"]).execute()

    result = {"checked": len(rows), "alive": alive_count, "deactivated": deactivated}

    # 나중에 어드민 대시보드에서 추이 그래프로 보여줄 수 있게 기록.
    try:
        sb.table("station_health_log").insert({
            "type": "radio",
            "checked": result["checked"],
            "alive": result["alive"],
            "deactivated": result["deactivated"],
        }).execute()
    except Exception:
        pass  # 로그 기록 실패는 헬스체크 자체 실패로 취급하지 않는다

    return result
