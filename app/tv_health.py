"""
`tv_channels`의 URL들을 주기적으로 점검해서 죽은 링크를 걸러낸다.

한 번에 전체(만 개 이상)를 다 점검하면 방송사 서버들한테도 부담이고
우리 서버 리소스도 많이 쓰니, 매 실행마다 BATCH_SIZE만큼만 —
가장 오래 점검 안 한 것부터 우선순위로 — 확인한다. 스케줄러가
주기적으로 이 함수를 호출하면, 전체 목록을 계속 돌아가면서 점검하게
된다.

연속 FAIL_THRESHOLD번 실패해야 is_active=false로 내린다 (일시적인
네트워크 문제로 죽은 걸로 오판하지 않기 위함). 한 번이라도 성공하면
연속 실패 카운트가 리셋되고 다시 is_active=true가 된다 — 방송사가
서버를 잠깐 내렸다가 복구하는 경우를 감안한 설계.
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
            res = await client.head(url, follow_redirects=True, timeout=TIMEOUT_SECONDS)
            if res.status_code < 400:
                return True
            # 일부 스트리밍 서버는 HEAD를 안 받아줘서 GET으로 한 번 더 시도
            res = await client.get(url, follow_redirects=True, timeout=TIMEOUT_SECONDS)
            return res.status_code < 400
    except (httpx.HTTPError, httpx.TimeoutException):
        return False


async def run_health_check_batch() -> dict:
    sb = get_supabase()
    res = (
        sb.table("tv_channels")
        .select("id, url, consecutive_fail_count")
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
            results[row["id"]] = await _check_one(row["url"])

    await asyncio.gather(*(worker(r) for r in rows))

    now = datetime.now(timezone.utc).isoformat()
    alive_count = 0
    deactivated = 0
    for row in rows:
        ok = results.get(row["id"], False)
        if ok:
            alive_count += 1
            sb.table("tv_channels").update({
                "is_active": True,
                "consecutive_fail_count": 0,
                "last_checked_at": now,
                "last_ok_at": now,
            }).eq("id", row["id"]).execute()
        else:
            fails = row["consecutive_fail_count"] + 1
            update = {"consecutive_fail_count": fails, "last_checked_at": now}
            if fails >= FAIL_THRESHOLD:
                update["is_active"] = False
                deactivated += 1
            sb.table("tv_channels").update(update).eq("id", row["id"]).execute()

    result = {"checked": len(rows), "alive": alive_count, "deactivated": deactivated}

    # 나중에 어드민 대시보드에서 추이 그래프로 보여줄 수 있게 기록.
    try:
        sb.table("station_health_log").insert({
            "type": "tv",
            "checked": result["checked"],
            "alive": result["alive"],
            "deactivated": result["deactivated"],
        }).execute()
    except Exception:
        pass  # 로그 기록 실패는 헬스체크 자체 실패로 취급하지 않는다

    return result
