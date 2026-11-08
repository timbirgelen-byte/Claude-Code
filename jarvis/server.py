"""J.A.R.V.I.S. Webserver: liefert die HUD-Oberfläche und streamt Agent-Events per SSE."""

from __future__ import annotations

import base64
import hmac
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import EFFORT, MODEL, Session, run_turn

STATIC = Path(__file__).parent / "static"
MAX_SESSIONS = 200

PASSWORD = os.getenv("JARVIS_PASSWORD", "")

app = FastAPI(title="J.A.R.V.I.S.")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
sessions: dict[str, Session] = {}


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Passwortschutz per Browser-Login (HTTP Basic), aktiv wenn JARVIS_PASSWORD gesetzt ist.

    Benutzername ist egal, nur das Passwort zählt.
    """
    if PASSWORD:
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                _, _, given = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                ok = hmac.compare_digest(given.encode(), PASSWORD.encode())
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response(
                "Zugang verweigert.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="JARVIS", charset="UTF-8"'},
            )
    return await call_next(request)


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
async def chat(req: ChatRequest, x_jarvis_key: str | None = Header(default=None)) -> StreamingResponse:
    user_key = (x_jarvis_key or "").strip() or None
    if user_key and len(user_key) > 300:
        raise HTTPException(400, "API-Key ist zu lang.")
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
            async for ev in run_turn(session, req.message, user_key):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
