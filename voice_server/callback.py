"""The screening transcript callback to the app."""
import asyncio
import logging

import httpx
from myvoiceai import SessionResult

from voice_server import settings, store
from voice_server.models import InterviewConfig

logger = logging.getLogger("voice_agent.callback")

CALLBACK_PATH = "/api/internal/interview-transcript"
CALLBACK_RETRY_DELAYS = (1, 5, 15)
CALLBACK_TIMEOUT_SECONDS = 10
RESULT_TTL_SECONDS = 24 * 60 * 60
# The app's limits (transcriptCallbackSchema); anything longer would get the whole callback rejected.
MAX_CALLBACK_TURNS = 200
MAX_TURN_TEXT_CHARS = 4000
CALLBACK_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests swap in a mock transport
_sleep = asyncio.sleep  # tests swap this to skip the retry waits


def callback_payload(sid: str, cfg: InterviewConfig, result: SessionResult | None) -> dict:
    """The body the app's interview-transcript route expects. No result (the
    socket was refused, or the session raised) is an error with no turns."""
    if result is None:
        return {"voiceSessionId": sid, "interviewId": cfg.interview_id, "turns": [], "endReason": "error", "durationSeconds": 0}
    turns = [
        {
            "turn_id": int(turn["turn_id"]),
            "timestamp": float(turn["timestamp"]),
            "user": str(turn["user"] or "")[:MAX_TURN_TEXT_CHARS],
            "assistant": None if turn["assistant"] is None else str(turn["assistant"])[:MAX_TURN_TEXT_CHARS],
            "interrupted": bool(turn["interrupted"]),
        }
        for turn in result.turns[:MAX_CALLBACK_TURNS]
    ]
    return {
        "voiceSessionId": sid,
        "interviewId": cfg.interview_id,
        "turns": turns,
        "endReason": result.end_reason,
        "durationSeconds": int(round(result.duration_seconds)),
    }


async def _post_callback(payload: dict) -> str:
    """One delivery attempt: "ok", "retry" (network error or 5xx/429) or "fail" (other 4xx)."""
    try:
        async with httpx.AsyncClient(transport=CALLBACK_TRANSPORT, timeout=CALLBACK_TIMEOUT_SECONDS) as http:
            response = await http.post(
                settings.APP_CALLBACK_URL.rstrip("/") + CALLBACK_PATH,
                json=payload,
                headers={"x-voice-callback-secret": settings.VOICE_CALLBACK_SECRET},
            )
    except httpx.HTTPError as error:
        logger.warning(f"Transcript callback failed: {error!r}")
        return "retry"
    if response.is_success:
        return "ok"
    logger.warning(f"Transcript callback got HTTP {response.status_code}")
    return "retry" if response.status_code >= 500 or response.status_code == 429 else "fail"


async def send_transcript_callback(sid: str, payload: dict):
    """Delivers the payload to the app, retrying after 1, 5 and 15 seconds.
    If it can't be delivered, it's kept in Redis for 24 hours so the app's
    sweeper can fetch it from GET /sessions/{sid}/result."""
    if settings.APP_CALLBACK_URL and settings.VOICE_CALLBACK_SECRET:
        for delay in (*CALLBACK_RETRY_DELAYS, None):
            outcome = await _post_callback(payload)
            if outcome == "ok":
                return
            if outcome == "fail" or delay is None:
                break
            await _sleep(delay)
    else:
        logger.error("APP_CALLBACK_URL or VOICE_CALLBACK_SECRET is not set; keeping the transcript for the sweeper.")
    try:
        await store.save_result(sid, payload, RESULT_TTL_SECONDS)
    except Exception as redis_err:
        logger.error(f"Failed to store the undelivered transcript for {sid}: {redis_err}")
