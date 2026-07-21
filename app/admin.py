"""Operator endpoints. Not linked anywhere, not in any public docs.

Auth is a single bearer token from the ADMIN_TOKEN Railway variable. If that
variable is unset the endpoints refuse everything: an unset secret must fail
closed, never open. Comparison is constant-time so the token cannot be guessed
a character at a time by timing the response.
"""
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app import flags
from app.config import get_settings

log = logging.getLogger("dreamteam.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


def _auth(authorization: str | None) -> None:
    expected = get_settings().admin_token
    if not expected:
        log.warning("admin call refused: ADMIN_TOKEN is not configured")
        raise HTTPException(401, "unauthorized")
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "unauthorized")


class FlagUpdate(BaseModel):
    name: str
    enabled: bool


@router.get("/flags")
async def list_flags(authorization: str | None = Header(default=None)):
    """Effective value of every flag and where it came from (db/env/default)."""
    _auth(authorization)
    return {"flags": flags.snapshot()}


@router.post("/flags")
async def set_flag(update: FlagUpdate, authorization: str | None = Header(default=None)):
    """Upsert a flag into the DB, where it then outranks the env variable."""
    _auth(authorization)
    try:
        await flags.set_flag(update.name, update.enabled)
    except KeyError:
        raise HTTPException(400, f"unknown flag '{update.name}'")
    return {"flags": flags.snapshot()}


@router.delete("/flags/{name}")
async def clear_flag(name: str, authorization: str | None = Header(default=None)):
    """Remove the DB row so the flag reverts to its env/default value."""
    _auth(authorization)
    try:
        await flags.clear_flag(name)
    except KeyError:
        raise HTTPException(400, f"unknown flag '{name}'")
    return {"flags": flags.snapshot()}
