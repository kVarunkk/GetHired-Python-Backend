import os
import sys
from pathlib import Path

import dotenv
import fakeredis
import pytest
from fastapi.testclient import TestClient

# voice_server.settings reads its settings at import time. Set them here and keep the local
# .env (real keys, real Upstash) out of the tests.
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ.update({
    "BACKEND_SECRET": "test-backend-secret",
    "ALLOWED_ORIGINS": "",
    "MAX_ACTIVE_INTERVIEWS": "10",
    "GEMINI_API_KEY": "test-gemini-key",
    "DEEPGRAM_API_KEY": "test-deepgram-key",
    "UPSTASH_REDIS_REST_URL": "https://redis.invalid",
    "UPSTASH_REDIS_REST_TOKEN": "test-token",
    "OTEL_EXPORTER_ENDPOINT": "https://otel.invalid/v1/traces",
    "OTEL_EXPORTER_API_KEY": "test-otel-key",
    "APP_CALLBACK_URL": "https://app.invalid",
    "VOICE_CALLBACK_SECRET": "test-callback-secret",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from voice_server import session, store  # noqa: E402
from myvoiceai import SessionResult  # noqa: E402

SECRET_HEADERS = {"x-backend-secret": "test-backend-secret"}
INTERVIEW_ID = "7d2c8f3e-9a41-4b6e-8f0a-2c5d1e9b4a10"


def practice_config(**overrides) -> dict:
    cfg = {
        "candidate_name": "Asha Rao",
        "profile": "Five years of backend work in Go and Postgres.",
        "job_title": "Backend Engineer",
        "job_description": "Build and run our payments APIs.",
        "interview_id": INTERVIEW_ID,
    }
    cfg.update(overrides)
    return cfg


class VoiceSessions:
    """Stands in for myvoiceai.run_voice_session: records each call's
    keyword arguments and returns `result`."""

    def __init__(self):
        self.calls: list[dict] = []
        self.result: SessionResult | None = SessionResult(
            turns=[], end_reason="client_disconnect", started_at=1.0, ended_at=2.0, duration_seconds=1.0,
        )
        self.raises: BaseException | None = None
        # Awaited with the call's kwargs while the session is "live".
        self.during = None

    async def __call__(self, websocket, **kwargs):
        self.calls.append(kwargs)
        if self.during is not None:
            await self.during(kwargs)
        if self.raises is not None:
            raise self.raises
        await websocket.close()  # like the real session's cleanup
        return self.result


@pytest.fixture
def redis_server():
    return fakeredis.FakeServer()


@pytest.fixture
def redis(redis_server, monkeypatch):
    """A sync view of the fake Redis the app uses, for setting up and checking state."""
    monkeypatch.setattr(store, "redis", fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True))
    return fakeredis.FakeRedis(server=redis_server, decode_responses=True)


@pytest.fixture
def voice(monkeypatch):
    sessions = VoiceSessions()
    monkeypatch.setattr(session, "run_voice_session", sessions)
    return sessions


@pytest.fixture
def client(redis, voice):
    with TestClient(main.app) as test_client:
        yield test_client


def create_session(client, cfg=None, headers=SECRET_HEADERS) -> str:
    response = client.post("/sessions", json=cfg or practice_config(), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def run_session(client, sid: str):
    with client.websocket_connect(f"/ws/interview?session_id={sid}"):
        pass
