"""Environment settings and limits. Read at import time."""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("voice_agent.settings")

BACKEND_SECRET = os.getenv("BACKEND_SECRET")
ALLOWED_ORIGINS = {o for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o}
INTERVIEW_SESSION_TTL = 350
MAX_ACTIVE_INTERVIEWS = int(os.getenv("MAX_ACTIVE_INTERVIEWS", "10"))


def default_reserved_slots(max_active: int) -> int:
    return int(max_active * 0.3)


# Slots only screening interviews may use, so practice traffic can't block a
# candidate on a deadline: practice is refused at MAX_ACTIVE_INTERVIEWS - reserved.
SCREENING_RESERVED_SLOTS = int(os.getenv("SCREENING_RESERVED_SLOTS") or default_reserved_slots(MAX_ACTIVE_INTERVIEWS))
MAX_SESSION_CREATES_PER_MIN = int(os.getenv("MAX_SESSION_CREATES_PER_MIN", "60"))
SESSION_CREATES_PER_INTERVIEW_PER_MIN = 5
# Each live session holds an `active:{sid}` key that expires on its own if the
# server dies, refreshed while the session runs.
ACTIVE_REFRESH_SECONDS = 60
ACTIVE_KEY_GRACE_SECONDS = 60
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
# Where screening transcripts are sent, and the secret the app checks.
APP_CALLBACK_URL = os.getenv("APP_CALLBACK_URL", "")
VOICE_CALLBACK_SECRET = os.getenv("VOICE_CALLBACK_SECRET", "")
OTEL_EXPORTER_ENDPOINT = os.getenv("OTEL_EXPORTER_ENDPOINT", "")
OTEL_EXPORTER_API_KEY = os.getenv("OTEL_EXPORTER_API_KEY", "")


def require_settings():
    """Refuses to start without the backend secret; warns about missing
    settings that only screening interviews need."""
    if not BACKEND_SECRET:
        raise RuntimeError("BACKEND_SECRET is not set; the voice server won't start without it.")
    for name in ("APP_CALLBACK_URL", "VOICE_CALLBACK_SECRET"):
        if not globals()[name]:
            logger.warning(f"{name} is not set; screening transcripts will only be kept for the sweeper.")


def otel_exporters() -> list[dict]:
    if not OTEL_EXPORTER_ENDPOINT:
        return []
    exporter: dict = {"endpoint": OTEL_EXPORTER_ENDPOINT}
    if OTEL_EXPORTER_API_KEY:
        exporter["headers"] = {"x-honeycomb-team": OTEL_EXPORTER_API_KEY}
    return [exporter]
