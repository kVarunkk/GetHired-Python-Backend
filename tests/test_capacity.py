"""Session-create rate limits, active-session keys and screening's reserved capacity (SIT-017)."""
import asyncio
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from voice_server import callback, settings
from conftest import SECRET_HEADERS, create_session, practice_config, run_session
from test_screening_config import screening_config


def other_interview(n: int) -> str:
    return f"7d2c8f3e-9a41-4b6e-8f0a-{n:012d}"


def fill(redis, n: int, ttl: int = 600):
    for i in range(n):
        redis.set(f"active:existing-{i}", "practice", ex=ttl)


def refused_code(client, sid: str) -> int | None:
    try:
        run_session(client, sid)
    except WebSocketDisconnect as closed:
        return closed.code
    return None


@pytest.fixture(autouse=True)
def no_callbacks(monkeypatch):
    async def ignore(sid, payload):
        pass

    monkeypatch.setattr(callback, "send_transcript_callback", ignore)


# --- rate limits -------------------------------------------------------------

def test_the_limit_is_per_interview_not_per_caller(client):
    for _ in range(5):
        create_session(client)

    assert client.post("/sessions", json=practice_config(), headers=SECRET_HEADERS).status_code == 429
    create_session(client, practice_config(interview_id=other_interview(2)))


def test_global_ceiling_on_session_creates(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_SESSION_CREATES_PER_MIN", 3)
    for n in range(3):
        create_session(client, practice_config(interview_id=other_interview(n)))

    response = client.post("/sessions", json=practice_config(interview_id=other_interview(9)), headers=SECRET_HEADERS)

    assert response.status_code == 429


def test_rate_limit_keys_expire_after_a_minute(client, redis):
    create_session(client)

    assert 0 < redis.ttl(f"ratelimit:interview:{practice_config()['interview_id']}") <= 60
    assert 0 < redis.ttl("ratelimit:sessions") <= 60


# --- active-session keys -----------------------------------------------------

def test_a_live_session_has_an_active_key_that_is_removed_after(client, voice, redis):
    seen = {}

    async def during(kwargs):
        seen["keys"] = redis.keys("active:*")
        seen["ttl"] = redis.ttl(f"active:{sid}")

    voice.during = during
    sid = create_session(client, screening_config(max_minutes=10))
    run_session(client, sid)

    assert seen["keys"] == [f"active:{sid}"]
    assert 10 * 60 + 30 + 60 - 5 < seen["ttl"] <= 10 * 60 + 30 + 60
    assert redis.keys("active:*") == []
    assert redis.get("active_interviews") is None  # the old counter is gone


def test_a_practice_key_lives_for_the_default_session_length(client, voice, redis):
    seen = {}

    async def during(kwargs):
        seen["ttl"] = redis.ttl(f"active:{sid}")

    voice.during = during
    sid = create_session(client)
    run_session(client, sid)

    assert 300 + 60 - 5 < seen["ttl"] <= 300 + 60


def test_the_active_key_is_refreshed_while_the_session_runs(client, voice, redis, monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_REFRESH_SECONDS", 0.05)
    seen = {}

    async def during(kwargs):
        redis.expire(f"active:{sid}", 2)
        await asyncio.sleep(0.2)
        seen["ttl"] = redis.ttl(f"active:{sid}")

    voice.during = during
    sid = create_session(client)
    with client.websocket_connect(f"/ws/interview?session_id={sid}") as ws:
        assert ws.receive()["type"] == "websocket.close"  # waits until the session ends

    assert seen["ttl"] > 300


def test_an_orphaned_key_expires_and_frees_its_slot(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "MAX_ACTIVE_INTERVIEWS", 1)
    monkeypatch.setattr(settings, "SCREENING_RESERVED_SLOTS", 0)
    redis.set("active:crashed", "practice", px=100)  # its server died without cleaning up

    assert refused_code(client, create_session(client)) == 1013
    time.sleep(0.15)
    assert refused_code(client, create_session(client)) is None


# --- reserved screening capacity ---------------------------------------------

def test_default_reservation_is_30_percent():
    assert settings.default_reserved_slots(10) == 3
    assert settings.default_reserved_slots(1) == 0


def test_practice_is_refused_when_only_reserved_slots_are_left(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "MAX_ACTIVE_INTERVIEWS", 10)
    monkeypatch.setattr(settings, "SCREENING_RESERVED_SLOTS", 3)
    fill(redis, 7)

    assert refused_code(client, create_session(client)) == 1013
    assert refused_code(client, create_session(client, screening_config(interview_id=other_interview(1)))) is None


def test_practice_is_accepted_below_the_reserve(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "MAX_ACTIVE_INTERVIEWS", 10)
    monkeypatch.setattr(settings, "SCREENING_RESERVED_SLOTS", 3)
    fill(redis, 6)

    assert refused_code(client, create_session(client)) is None


def test_screening_is_refused_at_the_maximum(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "MAX_ACTIVE_INTERVIEWS", 10)
    monkeypatch.setattr(settings, "SCREENING_RESERVED_SLOTS", 3)
    fill(redis, 10)

    assert refused_code(client, create_session(client, screening_config())) == 1013
