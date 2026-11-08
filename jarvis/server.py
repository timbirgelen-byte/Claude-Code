"""J.A.R.V.I.S. Webserver: liefert die HUD-Oberfläche und streamt Agent-Events per SSE."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import EFFORT, MODEL, Session, run_turn

STATIC = Path(__file__).parent / "static"
MAX_SESSIONS = 200

app = FastAPI(title="J.A.R.V.I.S.")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
sessions: dict[str, Session] = {}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
async def info() -> dict:
    return {
        "model": MODEL,
        "effort": EFFORT,
        "api_key": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
    }


@app.post("/api/reset")
async def reset(body: dict) -> dict:
    sessions.pop(str(body.get("session_id")), None)
    return {"ok": True}


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    sid = req.session_id or uuid.uuid4().hex
    session = sessions.get(sid)
    if session is None:
        if len(sessions) >= MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))  # älteste Sitzung verwerfen
        session = sessions[sid] = Session()
    if session.lock.locked():
        raise HTTPException(409, "Es läuft bereits eine Anfrage in dieser Sitzung.")

    async def events():
        async with session.lock:
            yield f"data: {json.dumps({'type': 'session', 'id': sid})}\n\n"
            async for ev in run_turn(session, req.message):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
