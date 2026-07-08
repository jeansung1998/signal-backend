"""
iptv-org (오픈소스, 방송사가 직접 공개한 스트림만 모아둔 프로젝트)
데이터를 가져와서 `tv_channels` 테이블에 채워넣는다.

- DMCA/성인물로 차단목록에 오른 채널은 처음부터 제외
- 폐국(closed) 표시된 채널도 제외
- 공식 채널 레코드에 연결 안 된(channel id가 없는) 스트림도 제외
  — 출처가 불분명해서 검증이 어려움
- 채널당 대표 스트림 1개만 저장 (같은 채널에 화질별로 여러 스트림이
  있는 경우 첫 번째 것만 사용, 나중에 필요하면 화질 선택 기능으로 확장 가능)

수동으로 한 번 돌리거나(/tv/ingest), 나중에 주기적으로(예: 매주)
다시 돌려서 iptv-org 쪽에 새로 추가된 채널을 반영할 수 있다.
"""
import httpx

from app.database import get_supabase

IPTV_STREAMS_URL = "https://raw.githubusercontent.com/iptv-org/api/gh-pages/streams.json"
IPTV_CHANNELS_URL = "https://raw.githubusercontent.com/iptv-org/api/gh-pages/channels.json"
IPTV_BLOCKLIST_URL = "https://raw.githubusercontent.com/iptv-org/api/gh-pages/blocklist.json"


async def ingest_iptv_channels() -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        streams_res = await client.get(IPTV_STREAMS_URL)
        channels_res = await client.get(IPTV_CHANNELS_URL)
        blocklist_res = await client.get(IPTV_BLOCKLIST_URL)
    streams = streams_res.json()
    channels = {c["id"]: c for c in channels_res.json()}
    blocked_ids = {b["channel"] for b in blocklist_res.json()}

    rows = []
    seen_ids = set()
    for s in streams:
        cid = s.get("channel")
        if not cid or cid in blocked_ids:
            continue
        channel = channels.get(cid)
        if not channel or channel.get("closed"):
            continue
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        categories = channel.get("categories") or []
        rows.append({
            "id": cid,
            "name": channel.get("name") or s.get("title") or cid,
            "country_code": channel.get("country"),
            "category": categories[0] if categories else None,
            "url": s["url"],
            "quality": s.get("quality"),
        })

    sb = get_supabase()
    # 배치 upsert — 한 번에 500개씩 (Supabase 요청 크기 제한 회피)
    for i in range(0, len(rows), 500):
        sb.table("tv_channels").upsert(rows[i:i + 500]).execute()

    return {"ingested": len(rows)}
