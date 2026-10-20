import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, status, HTTPException, Request

from voice_server import callback, session, settings, store
from voice_server.auth import backend_secret_ok
from voice_server.models import InterviewConfig

logger = logging.getLogger("voice_agent.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.require_settings()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/sessions")
async def create_session(cfg: InterviewConfig, request: Request):
    if not backend_secret_ok(request):
        logger.warning(f"Unauthorized request from {request.client.host if request.client else 'unknown'}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    if await store.session_creates_over_limit(cfg.interview_id):
        logger.warning(f"Session rate limit exceeded for interview {cfg.interview_id}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later."
        )

    return {"session_id": await store.create_session(cfg)}


@app.get("/sessions/{sid}/result")
async def get_session_result(sid: str, request: Request):
    """A screening callback that couldn't be delivered, for the app's sweeper."""
    if not backend_secret_ok(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    result = await store.get_result(sid)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return result


@app.delete("/sessions/{sid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(sid: str, request: Request):
    """Removes a session the app created but won't use."""
    if not backend_secret_ok(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    await store.delete_session(sid)


@app.websocket("/ws/interview")
async def ws_interview_handler(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if settings.ALLOWED_ORIGINS and origin and origin not in settings.ALLOWED_ORIGINS:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    session_id = websocket.query_params.get("session_id", "")
    cfg = await store.take_session(session_id)
    if cfg is None:
        logger.warning(f"Invalid or expired session attempt for ID: {session_id}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Check active concurrency limits to protect free tier server memory/CPU
    limit = settings.MAX_ACTIVE_INTERVIEWS
    if cfg.mode != "screening":
        limit -= settings.SCREENING_RESERVED_SLOTS
    if await store.count_active_sessions() >= limit:
        logger.warning(f"Max concurrent {cfg.mode} interviews reached. Rejecting connection.")
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        if cfg.mode == "screening":
            # The app gives the attempt back for an error with no answers.
            await callback.send_transcript_callback(session_id, callback.callback_payload(session_id, cfg, None))
        return

    await session.run_interview(websocket, cfg, session_id)
