"""Today's practice interview, pinned before the screening changes (SIT-014).

A config with no `mode` must keep behaving exactly like this.
"""
import json
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from voice_server import prompts, settings
from voice_server.models import InterviewConfig
from conftest import INTERVIEW_ID, SECRET_HEADERS, create_session, practice_config, run_session

PRACTICE_PROMPT = (Path(__file__).parent / "snapshots" / "practice_prompt.txt").read_text(encoding="utf-8")


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_practice_prompt_snapshot():
    assert prompts.build_interview_prompt(InterviewConfig(**practice_config())) == PRACTICE_PROMPT


@pytest.mark.parametrize("headers", [{}, {"x-backend-secret": "wrong"}])
def test_create_session_needs_the_backend_secret(client, headers):
    response = client.post("/sessions", json=practice_config(), headers=headers)

    assert response.status_code == 401


def test_create_session_stores_the_config_for_350_seconds(client, redis):
    sid = create_session(client)

    stored = json.loads(redis.get(f"session:{sid}"))
    assert stored["cfg"]["candidate_name"] == "Asha Rao"
    assert stored["cfg"]["interview_id"] == INTERVIEW_ID
    assert 340 < redis.ttl(f"session:{sid}") <= 350


def test_create_session_validates_the_config(client):
    response = client.post("/sessions", json=practice_config(profile="x" * 8001), headers=SECRET_HEADERS)

    assert response.status_code == 422


def test_create_session_ignores_unknown_fields(client):
    create_session(client, practice_config(something_new="ignored"))


def test_sixth_session_for_an_interview_in_a_minute_is_rate_limited(client):
    for _ in range(5):
        create_session(client)

    response = client.post("/sessions", json=practice_config(), headers=SECRET_HEADERS)

    assert response.status_code == 429


def test_websocket_runs_the_practice_session(client, voice):
    run_session(client, create_session(client))

    assert len(voice.calls) == 1
    kwargs = voice.calls[0]
    stable = {k: v for k, v in kwargs.items() if k not in {"session_id", "otel_exporter_endpoint", "otel_exporter_headers", "otel_exporters"}}
    assert stable == {
        "system_prompt": PRACTICE_PROMPT,
        "greeting_message": "Hi Asha Rao, I'm your interviewer for the Backend Engineer role. "
                            "Whenever you're ready, tell me a bit about yourself.",
        "endpointing": 1200,
        "utterance_end": 2500,
        "stable_interim_secs": 1.5,
        "stable_interim_secs_no_punct": 3.5,
        "inactivity_timeout_seconds": 10,
        "model": "gemini/gemini-3.1-flash-lite",
        "llm_provider_api_key": "test-gemini-key",
        "deepgram_api_key": "test-deepgram-key",
        "tracing": True,
    }


def test_unknown_session_is_refused_with_1008(client, voice):
    with pytest.raises(WebSocketDisconnect) as closed:
        run_session(client, "no-such-session")

    assert closed.value.code == 1008
    assert voice.calls == []


def test_a_session_is_single_use(client, voice):
    sid = create_session(client)
    run_session(client, sid)

    with pytest.raises(WebSocketDisconnect) as closed:
        run_session(client, sid)

    assert closed.value.code == 1008
    assert len(voice.calls) == 1


def test_disallowed_origin_is_refused_with_1008(client, voice, monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", {"https://gethired.devhub.co.in"})
    sid = create_session(client)

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"/ws/interview?session_id={sid}", headers={"origin": "https://evil.example"}):
            pass

    assert closed.value.code == 1008
    assert voice.calls == []


def test_a_session_error_is_survived(client, voice):
    voice.raises = RuntimeError("boom")

    run_session(client, create_session(client))

    assert len(voice.calls) == 1
    run_session(client, create_session(client))  # the server still takes new sessions
