"""Runs a voice interview over an accepted session's websocket."""
import logging

from fastapi import WebSocket, WebSocketDisconnect, status
from myvoiceai import SessionResult, run_voice_session
from myvoiceai.lib.constants import DEFAULT_MAX_SESSION_SECONDS

from voice_server import callback, settings, store
from voice_server.models import InterviewConfig
from voice_server.prompts import session_options

logger = logging.getLogger("voice_agent.session")


async def _converse(websocket: WebSocket, cfg: InterviewConfig, session_id: str, options: dict) -> SessionResult | None:
    try:
        await websocket.accept()
        logger.info(f"WebSocket accepted for candidate {cfg.candidate_name} ({cfg.job_title})")

        return await run_voice_session(
            websocket=websocket,
            endpointing=1200,
            utterance_end=2500,
            stable_interim_secs=1.5,
            stable_interim_secs_no_punct=3.5,
            model="gemini/gemini-3.1-flash-lite",
            **options,
            llm_provider_api_key=settings.GEMINI_API_KEY,
            deepgram_api_key=settings.DEEPGRAM_API_KEY,
            # Both ids, so a trace can be found from the interview or from this attempt.
            session_id=f"{cfg.interview_id}:{session_id}",
            tracing=True,
            otel_exporters=settings.otel_exporters(),
        )
    except WebSocketDisconnect:
        logger.info(f"Session {session_id} disconnected normally by client.")
    except Exception as e:
        logger.error(f"Unexpected error in voice session {session_id}: {e}", exc_info=True)
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except RuntimeError:
            pass
    return None


async def run_interview(websocket: WebSocket, cfg: InterviewConfig, session_id: str):
    """Runs the session in an active slot; a screening session then reports
    its transcript (or an error) to the app."""
    options = session_options(cfg)
    active_ttl = int(options.get("max_session_seconds", DEFAULT_MAX_SESSION_SECONDS)) + settings.ACTIVE_KEY_GRACE_SECONDS
    result: SessionResult | None = None
    try:
        async with store.active_slot(session_id, cfg.mode, active_ttl):
            result = await _converse(websocket, cfg, session_id, options)
    finally:
        if cfg.mode == "screening":
            await callback.send_transcript_callback(session_id, callback.callback_payload(session_id, cfg, result))
