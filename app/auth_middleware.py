from datetime import datetime, timezone
import hmac

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import (
    SESSION_COOKIE_NAME,
    hash_session_token,
)


class AuthenticationMiddleware(BaseHTTPMiddleware):
    PUBLIC_PATHS = {
        "/health",
        "/version",
        "/metrics",
        "/login",
        "/api/auth/login",
    }

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request, call_next):
        path = request.url.path

        if path in self.PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        database = getattr(request.app.state, "database", None)

        session = None

        if token and database is not None:
            token_hash = hash_session_token(token)
            session = database.get_session(token_hash)

            if session is not None:
                try:
                    expires_at = datetime.fromisoformat(session["expires_at"])

                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)

                    if expires_at <= datetime.now(timezone.utc):
                        database.delete_session(token_hash)
                        session = None
                except (ValueError, TypeError, KeyError):
                    database.delete_session(token_hash)
                    session = None

        if session is None:
            if token and database is not None:
                database.delete_session(hash_session_token(token))

            if path.startswith("/api/"):
                return JSONResponse(
                    {"detail": "Authentication required."},
                    status_code=401,
                )

            return RedirectResponse("/login", status_code=303)

        request.state.auth_session = session

        if request.method.upper() not in self.SAFE_METHODS:
            supplied_csrf = request.headers.get("X-CSRF-Token", "")

            if not hmac.compare_digest(
                supplied_csrf,
                session["csrf_token"],
            ):
                return JSONResponse(
                    {"detail": "Invalid or missing CSRF token."},
                    status_code=403,
                )

        return await call_next(request)
