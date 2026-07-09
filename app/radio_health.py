"""
`radio_stations`의 스트림 URL들을 주기적으로 점검해서 죽은 방송국을
걸러낸다. tv_health.py와 동일한 패턴 — 배치로 나눠서 점검하고,
연속 FAIL_THRESHOLD번 실패해야 is_active=false로 내린다.

라디오 스트림은 TV와 달리 계속 흘러나오는 오디오라서, 일반 GET으로
받으면 커넥션이 안 끊기고 계속 데이터를 받아버릴 수 있다. 그래서
`client.stream()`으로 열어서 상태 코드만 확인하고 바로 연결을 닫는다
(실제 오디오 바이트는 거의 안 받음).
"""
import asyncio
from datetime import datetime, timezone

import httpx

from app.database import get_supabase

BATCH_SIZE = 300
CONCURRENCY = 20
TIMEOUT_SECONDS = 8
FAIL_THRESHOLD = 3  # 연속 3번 실패하면 비활성화


async def _check_one(url: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            # 먼저 HEAD로 가볍게 시도 (일부 icecast/shoutcast 서버는 지원 안 함)
            try:
                res = await client.head(url, follow_redirects=True, timeout=TIMEOUT_SECONDS)
                if res.status_code < 400:
                    return True
            except (httpx.HTTPError, httpx.TimeoutException):
                pass

            # HEAD가 안 되면 스트림을 잠깐 열어서 상태 코드만 확인하고 즉시 닫는다.
            # (오디오 데이터를 실제로 다운로드하지 않도록 바로 close)
            async with client.stream(
                "GET", url, follow_redirects=True, timeout=TIMEOUT_SECONDS
            ) as res:
                return res.status_code < 400
    except (httpx.HTTPError, httpx.TimeoutException):
        return False


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
