"""
SIGNAL backend — FastAPI entry point.

Run locally:
    uvicorn main:app --reload --port 8000

Deploy target: Railway (same pattern as KOKO's toto-server).
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.routers import presence, users, match, radio, ws, travel, admin, translate_api, tv, tv_favorites, country_api
from app.tv_health import run_health_check_batch
from app.radio_health import run_health_check_batch as run_radio_health_check_batch

app = FastAPI(title="SIGNAL API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://(.*\.lovableproject\.com|signalearth\.kr|www\.signalearth\.kr)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(presence.router, prefix="/presence", tags=["presence"])
app.include_router(match.router, prefix="/match", tags=["match"])
app.include_router(radio.router, prefix="/radio", tags=["radio"])
app.include_router(ws.router, prefix="/ws", tags=["websocket"])
app.include_router(travel.router, tags=["travel"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(translate_api.router, tags=["translate"])
app.include_router(tv.router, prefix="/tv", tags=["tv"])
app.include_router(tv_favorites.router, tags=["tv_favorites"])
app.include_router(country_api.router, tags=["countries"])

scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def start_scheduler():
    import datetime

    # 1시간마다 오래 점검 안 된 TV 채널부터 300개씩 헬스체크.
    # 만 개 넘는 전체 목록을 한 번에 다 돌면 부담이 커서 이렇게
    # 조금씩 계속 순환하며 확인하는 방식으로 감.
    scheduler.add_job(run_health_check_batch, "interval", hours=1, id="tv_health_check")

    # 라디오도 동일한 방식(1시간마다 300개씩). TV와 같은 순간에
    # 동시에 돌면 서버 리소스가 겹치니, 시작 시각을 30분 뒤로
    # 밀어서 서로 어긋나게 돈다.
    radio_start = datetime.datetime.now() + datetime.timedelta(minutes=30)
    scheduler.add_job(
        run_radio_health_check_batch,
        "interval",
        hours=1,
        id="radio_health_check",
        next_run_time=radio_start,
    )
    scheduler.start()


@app.get("/health")
def health():
    return {"status": "ok"}
