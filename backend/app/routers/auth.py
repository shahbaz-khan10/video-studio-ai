"""Phase 15 — sign-in endpoints."""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models_workers  # noqa: F401
from ..database import get_db
from ..models_workers import UserAccount
from ..services import auth
from ..services.media import AppError

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)


def _user_out(user: UserAccount) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "is_admin": user.is_admin}


def _set_cookie(response: Response, user: UserAccount) -> None:
    response.set_cookie(
        auth.COOKIE, auth.make_token(user.id, user.session_version), max_age=int(auth.auth_settings.session_hours * 3600),
        httponly=True, samesite="lax", secure=auth.auth_settings.cookie_secure, path="/",
    )


@router.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)) -> dict:
    if not auth.enabled():
        return {"mode": "off", "user": None, "can_register": False}
    user = auth.user_from_request(request)
    has_users = (db.scalar(select(func.count()).select_from(UserAccount)) or 0) > 0
    return {
        "mode": "password",
        "user": {"id": user.id, "email": user.email, "display_name": user.display_name, "is_admin": user.is_admin} if user else None,
        "can_register": (not has_users) or auth.auth_settings.allow_registration or bool(user and user.is_admin),
        "first_user": not has_users,
    }


@router.post("/register")
def register(payload: Credentials, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    if not auth.enabled():
        raise AppError(409, "AUTH_DISABLED", "Sign-in is disabled (AUTH_MODE=off).")
    has_users = (db.scalar(select(func.count()).select_from(UserAccount)) or 0) > 0
    current = auth.user_from_request(request)
    if has_users and not auth.auth_settings.allow_registration and not (current and current.is_admin):
        raise AppError(403, "REGISTRATION_CLOSED", "Ask an administrator to create your account.")
    email = payload.email.strip().lower()
    if "@" not in email:
        raise AppError(422, "INVALID_EMAIL", "Enter a valid email address.")
    if db.scalar(select(UserAccount).where(UserAccount.email == email)):
        raise AppError(409, "EMAIL_TAKEN", "An account with this email already exists.")
    user = UserAccount(email=email, display_name=(payload.display_name or "").strip() or None,
                       password_hash=auth.hash_password(payload.password), is_admin=not has_users)
    db.add(user)
    db.commit()
    db.refresh(user)
    if current is None:
        _set_cookie(response, user)
    return _user_out(user)


@router.post("/login")
def login(payload: Credentials, response: Response, db: Session = Depends(get_db)) -> dict:
    if not auth.enabled():
        raise AppError(409, "AUTH_DISABLED", "Sign-in is disabled (AUTH_MODE=off).")
    user = db.scalar(select(UserAccount).where(UserAccount.email == payload.email.strip().lower()))
    if user is None or not user.active or not auth.verify_password(payload.password, user.password_hash):
        raise AppError(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")
    _set_cookie(response, user)
    return _user_out(user)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.post("/logout-everywhere")
def logout_everywhere(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    current = auth.user_from_request(request)
    if current is None:
        raise AppError(401, "UNAUTHENTICATED", "Sign in first.")
    user = db.get(UserAccount, current.id)
    user.session_version += 1  # invalidates every issued session
    db.commit()
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}
