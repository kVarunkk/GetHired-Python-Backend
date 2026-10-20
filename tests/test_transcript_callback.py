"""The screening transcript callback to the app, its retries and the Redis fallback (SIT-016)."""
import json
from pathlib import Path

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from voice_server import callback, settings
from voice_server.models import InterviewConfig
from conftest import INTERVIEW_ID, SECRET_HEADERS, create_session, practice_config, run_session
from myvoiceai import SessionResult
from test_screening_config import screening_config

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "transcript_callback.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
APP_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "job_application_agent_frontend" / "helpers" / "interview" / "screening" / "fixtures" / "transcript-callback.json"
)
CALLBACK_URL = "https://app.invalid/api/internal/interview-transcript"


class App:
    """The app's callback endpoint: answers with the queued status codes (or
    raises the queued exceptions), then 202."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0) if self.responses else 202
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"status": "recorded"})

    @property
    def payloads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def app(monkeypatch):
    endpoint = App()
    monkeypatch.setattr(callback, "CALLBACK_TRANSPORT", httpx.MockTransport(endpoint.handler))
    return endpoint


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(callback, "_sleep", fake_sleep)
    return waited


def result(turns=None, end_reason="completed", duration=612.4):
    return SessionResult(
        turns=turns if turns is not None else [dict(t) for t in FIXTURE["turns"]],
        end_reason=end_reason,
        started_at=1760000000.0,
        ended_at=1760000000.0 + duration,
        duration_seconds=duration,
    )


# --- payload -----------------------------------------------------------------

def test_payload_matches_the_shared_fixture():
    cfg = InterviewConfig(**screening_config())

    assert callback.callback_payload("sid-fixture", cfg, result()) == FIXTURE


@pytest.mark.skipif(not APP_FIXTURE_PATH.exists(), reason="the app checkout isn't next to this one")
def test_fixture_is_the_same_as_the_apps_copy():
    assert json.loads(APP_FIXTURE_PATH.read_text(encoding="utf-8")) == FIXTURE


def test_payload_caps_turns_and_text_to_the_apps_limits():
    cfg = InterviewConfig(**screening_config())
    long_turns = [
        {"turn_id": i, "timestamp": 1.0, "user": "u" * 5000, "assistant": "a" * 5000, "interrupted": False}
        for i in range(1, 251)
    ]

    payload = callback.callback_payload("sid", cfg, result(turns=long_turns))

    assert len(payload["turns"]) == 200
    assert len(payload["turns"][0]["user"]) == 4000
    assert len(payload["turns"][0]["assistant"]) == 4000


def test_no_result_is_an_error_with_no_turns():
    cfg = InterviewConfig(**screening_config())

    assert callback.callback_payload("sid", cfg, None) == {
        "voiceSessionId": "sid",
        "interviewId": INTERVIEW_ID,
        "turns": [],
        "endReason": "error",
        "durationSeconds": 0,
    }


# --- sending -----------------------------------------------------------------

def test_screening_session_calls_back_with_its_result(client, voice, app, sleeps):
    voice.result = result()
    sid = create_session(client, screening_config())

    run_session(client, sid)

    assert len(app.requests) == 1
    request = app.requests[0]
    assert str(request.url) == CALLBACK_URL
    assert request.headers["x-voice-callback-secret"] == "test-callback-secret"
    assert app.payloads[0] == {**FIXTURE, "voiceSessionId": sid}
    assert sleeps == []


def test_retries_after_1_5_and_15_seconds(client, voice, app, sleeps, redis):
    app.responses = [500, httpx.ConnectError("refused"), 503]
    sid = create_session(client, screening_config())

    run_session(client, sid)

    assert len(app.requests) == 4
    assert sleeps == [1, 5, 15]
    assert redis.get(f"result:{sid}") is None


def test_final_failure_stores_the_payload_for_24_hours(client, voice, app, sleeps, redis):
    app.responses = [500, 500, 500, 500]
    sid = create_session(client, screening_config())

    run_session(client, sid)

    assert len(app.requests) == 4
    assert json.loads(redis.get(f"result:{sid}")) == app.payloads[0]
    assert 86_390 < redis.ttl(f"result:{sid}") <= 86_400


def test_a_client_error_is_not_retried_but_is_stored(client, voice, app, sleeps, redis):
    app.responses = [400]
    sid = create_session(client, screening_config())

    run_session(client, sid)

    assert len(app.requests) == 1
    assert sleeps == []
    assert redis.get(f"result:{sid}") is not None


def test_without_a_callback_url_the_payload_is_stored(client, voice, app, sleeps, redis, monkeypatch):
    monkeypatch.setattr(settings, "APP_CALLBACK_URL", "")
    sid = create_session(client, screening_config())

    run_session(client, sid)

    assert app.requests == []
    assert redis.get(f"result:{sid}") is not None


def test_a_session_that_raises_calls_back_with_an_error(client, voice, app, sleeps):
    voice.raises = RuntimeError("boom")
    sid = create_session(client, screening_config())

    run_session(client, sid)

    assert app.payloads == [{"voiceSessionId": sid, "interviewId": INTERVIEW_ID, "turns": [], "endReason": "error", "durationSeconds": 0}]


def test_a_screening_socket_refused_for_capacity_calls_back_with_an_error(client, voice, app, sleeps, monkeypatch):
    monkeypatch.setattr(settings, "MAX_ACTIVE_INTERVIEWS", 0)
    sid = create_session(client, screening_config())

    with pytest.raises(WebSocketDisconnect) as closed:
        run_session(client, sid)

    assert closed.value.code == 1013
    assert voice.calls == []
    assert app.payloads == [{"voiceSessionId": sid, "interviewId": INTERVIEW_ID, "turns": [], "endReason": "error", "durationSeconds": 0}]


@pytest.mark.parametrize("problem", ["normal", "raises", "capacity"])
def test_practice_sessions_never_call_back(client, voice, app, sleeps, redis, monkeypatch, problem):
    if problem == "raises":
        voice.raises = RuntimeError("boom")
    if problem == "capacity":
        monkeypatch.setattr(settings, "MAX_ACTIVE_INTERVIEWS", 0)
    sid = create_session(client, practice_config())

    try:
        run_session(client, sid)
    except WebSocketDisconnect:
        pass

    assert app.requests == []
    assert redis.get(f"result:{sid}") is None


# --- result and delete endpoints ---------------------------------------------

@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.parametrize("headers", [{}, {"x-backend-secret": "wrong"}])
def test_result_and_delete_need_the_backend_secret(client, method, headers):
    path = "/sessions/some-sid/result" if method == "get" else "/sessions/some-sid"

    response = client.request(method.upper(), path, headers=headers)

    assert response.status_code == 401


def test_result_returns_the_stored_payload_or_404(client, redis):
    redis.set("result:sid-1", json.dumps(FIXTURE), ex=60)

    assert client.get("/sessions/sid-1/result", headers=SECRET_HEADERS).json() == FIXTURE
    assert client.get("/sessions/sid-2/result", headers=SECRET_HEADERS).status_code == 404


def test_delete_removes_an_unused_session(client, voice, redis):
    sid = create_session(client, screening_config())

    response = client.delete(f"/sessions/{sid}", headers=SECRET_HEADERS)

    assert response.status_code == 204
    assert redis.get(f"session:{sid}") is None
    with pytest.raises(WebSocketDisconnect) as closed:
        run_session(client, sid)
    assert closed.value.code == 1008
    assert voice.calls == []


def test_delete_of_an_unknown_session_is_still_204(client):
    assert client.delete("/sessions/never-made", headers=SECRET_HEADERS).status_code == 204
