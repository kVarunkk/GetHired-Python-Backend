"""SI-VS-007 fixes: OTLP exporters, the secret check, startup settings, the sid in traces (SIT-018)."""
import hmac

import pytest
from fastapi.testclient import TestClient

import main
from voice_server import auth, settings
from conftest import INTERVIEW_ID, SECRET_HEADERS, create_session, practice_config, run_session


def test_tracing_uses_otel_exporters(client, voice):
    run_session(client, create_session(client))

    kwargs = voice.calls[0]
    assert "otel_exporter_endpoint" not in kwargs
    assert "otel_exporter_headers" not in kwargs
    assert kwargs["otel_exporters"] == [{
        "endpoint": "https://otel.invalid/v1/traces",
        "headers": {"x-honeycomb-team": "test-otel-key"},
    }]


def test_no_exporter_without_an_endpoint(client, voice, monkeypatch):
    monkeypatch.setattr(settings, "OTEL_EXPORTER_ENDPOINT", "")

    run_session(client, create_session(client))

    assert voice.calls[0]["otel_exporters"] == []


def test_no_header_without_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_EXPORTER_API_KEY", "")

    assert settings.otel_exporters() == [{"endpoint": "https://otel.invalid/v1/traces"}]


def test_traces_carry_the_interview_id_and_the_sid(client, voice):
    sid = create_session(client)
    run_session(client, sid)

    assert voice.calls[0]["session_id"] == f"{INTERVIEW_ID}:{sid}"


@pytest.mark.parametrize("path,method", [("/sessions", "POST"), ("/sessions/x/result", "GET"), ("/sessions/x", "DELETE")])
def test_every_secret_check_uses_compare_digest(client, monkeypatch, path, method):
    compared = []
    real_compare = hmac.compare_digest

    def spy(a, b):
        compared.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    body = practice_config() if method == "POST" else None

    client.request(method, path, json=body, headers={"x-backend-secret": "wrong"})

    assert compared == [(b"wrong", b"test-backend-secret")]


def test_surrounding_whitespace_in_the_header_is_still_accepted(client):
    create_session(client, headers={"x-backend-secret": " test-backend-secret "})


def test_an_unset_secret_refuses_with_401_not_500(client, monkeypatch):
    monkeypatch.setattr(settings, "BACKEND_SECRET", None)

    response = client.post("/sessions", json=practice_config(), headers=SECRET_HEADERS)

    assert response.status_code == 401


def test_startup_fails_clearly_without_the_backend_secret(redis, voice, monkeypatch):
    monkeypatch.setattr(settings, "BACKEND_SECRET", "")

    with pytest.raises(RuntimeError, match="BACKEND_SECRET"):
        with TestClient(main.app):
            pass


def test_startup_warns_without_the_callback_settings(redis, voice, monkeypatch, caplog):
    monkeypatch.setattr(settings, "VOICE_CALLBACK_SECRET", "")

    with TestClient(main.app):
        pass

    assert "VOICE_CALLBACK_SECRET" in caplog.text
