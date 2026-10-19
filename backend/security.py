import hashlib
import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response


BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / ".qai-api-token"
SESSION_COOKIE = "qai_session"
DEFAULT_ORIGINS = {"http://127.0.0.1:8000", "http://localhost:8000"}
DEFAULT_HOSTS = {"127.0.0.1:8000", "localhost:8000"}


def _csv_setting(name: str, defaults: set[str]) -> set[str]:
    value = os.getenv(name)
    if value is None:
        return set(defaults)
    values = {item.strip() for item in value.split(",") if item.strip()}
    if not values:
        raise RuntimeError(f"{name} must contain at least one value.")
    return values


def allowed_origins() -> set[str]:
    return _csv_setting("QAI_FRONTEND_ORIGINS", DEFAULT_ORIGINS)


def allowed_hosts() -> set[str]:
    return _csv_setting("QAI_ALLOWED_API_HOSTS", DEFAULT_HOSTS)


def load_or_create_token() -> str:
    configured = os.getenv("QAI_API_TOKEN")
    if configured is not None:
        token = configured.strip()
        if len(token) < 32:
            raise RuntimeError("QAI_API_TOKEN must contain at least 32 characters.")
        return token

    try:
        if TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(token) < 32:
                raise RuntimeError("The local API token file is invalid.")
            return token
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        with TOKEN_FILE.open("x", encoding="utf-8") as handle:
            handle.write(token + "\n")
        return token
    except FileExistsError:
        raise RuntimeError(
            "The local API token was created concurrently; restart the API."
        ) from None
    except OSError as error:
        raise RuntimeError("The local API token could not be safely loaded.") from error


API_TOKEN = load_or_create_token()
_TOKEN_DIGEST = hashlib.sha256(API_TOKEN.encode("utf-8")).digest()


def _valid_host(request: Request) -> bool:
    return request.headers.get("host", "").lower() in {
        host.lower() for host in allowed_hosts()
    }


def _valid_origin(request: Request, *, required: bool) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return not required
    return origin in allowed_origins()


def _valid_bearer(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.casefold() != "bearer" or not value:
        return False
    return hmac.compare_digest(
        hashlib.sha256(value.strip().encode("utf-8")).digest(),
        _TOKEN_DIGEST,
    )


def _valid_session(request: Request) -> bool:
    value = request.cookies.get(SESSION_COOKIE)
    if not value or "." not in value:
        return False
    signature, nonce = value.split(".", 1)
    expected = hmac.new(
        API_TOKEN.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(
        signature,
        expected,
    )


def validate_request_context(request: Request, *, state_changing: bool = False) -> None:
    if not _valid_host(request):
        raise HTTPException(status_code=400, detail="Request host is not allowed.")
    if state_changing and not _valid_origin(request, required=False):
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")
    if not _valid_bearer(request) and not _valid_session(request):
        raise HTTPException(status_code=401, detail="Authorization required.")
    if _valid_session(request) and not _valid_origin(request, required=state_changing):
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")


def bootstrap_session(request: Request, response: Response) -> None:
    if not _valid_host(request) or not _valid_origin(request, required=True):
        raise HTTPException(status_code=403, detail="Trusted browser origin required.")
    nonce = secrets.token_urlsafe(24)
    session = hmac.new(
        API_TOKEN.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest() + "." + nonce
    is_secure = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto") == "https"
    )
    response.set_cookie(
        SESSION_COOKIE,
        session,
        httponly=True,
        samesite="strict",
        secure=is_secure,
        path="/",
    )
