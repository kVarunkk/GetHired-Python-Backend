import hmac

from fastapi import Request

from voice_server import settings


def backend_secret_ok(request: Request) -> bool:
    if not settings.BACKEND_SECRET:
        return False
    given = request.headers.get("x-backend-secret", "").strip()
    return hmac.compare_digest(given.encode(), settings.BACKEND_SECRET.strip().encode())
