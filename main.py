"""
SIGNAL backend — FastAPI entry point.

Run locally:
    uvicorn main:app --reload --port 8000

Deploy target: Railway (same pattern as KOKO's toto-server).
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import presence, users, match, radio, ws

app = FastAPI(title="SIGNAL API", version="0.1.0")

# TODO: narrow this to the actual frontend origin(s) before going to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(presence.router, prefix="/presence", tags=["presence"])
app.include_router(match.router, prefix="/match", tags=["match"])
app.include_router(radio.router, prefix="/radio", tags=["radio"])
app.include_router(ws.router, prefix="/ws", tags=["websocket"])

@app.get("/health")
def health():
    return {"status": "ok"}