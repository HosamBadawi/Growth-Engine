"""Simple password login for the local dashboard (password from .env)."""
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from engine.config import get_settings

COOKIE_NAME = "ge_auth"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="dashboard")


def make_token() -> str:
    return _serializer().dumps("ok")


def is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        return _serializer().loads(token) == "ok"
    except BadSignature:
        return False


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
