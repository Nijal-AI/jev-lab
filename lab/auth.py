"""Jetons signés HMAC : code de session + prénom, pas de comptes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from fastapi import Header, HTTPException

from . import config


def _sign(body: bytes) -> str:
    return hmac.new(config.SECRET_KEY.encode(), body, hashlib.sha256).hexdigest()


def make_token(name: str, role: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"n": name, "r": role}).encode())
    return f"{body.decode()}.{_sign(body)}"


def read_token(token: str) -> dict | None:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(body.encode()), sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body))
    except ValueError:
        return None
    return {"name": data["n"], "role": data["r"]}


def login(code: str, name: str) -> dict:
    code = code.strip()
    name = " ".join(name.split())[:40]
    if config.ADMIN_CODE and hmac.compare_digest(code, config.ADMIN_CODE):
        role = "admin"
        name = name or "admin"
    elif code.lower() in config.SESSION_CODES:
        role = "participant"
    else:
        raise HTTPException(401, "Code de session inconnu.")
    if not name:
        raise HTTPException(400, "Indique ton prénom.")
    return {"token": make_token(name, role), "name": name, "role": role}


def current_user(x_lab_token: str = Header(default="")) -> dict:
    user = read_token(x_lab_token)
    if not user:
        raise HTTPException(401, "Session expirée : reconnecte-toi.")
    return user


def admin_user(x_lab_token: str = Header(default="")) -> dict:
    user = current_user(x_lab_token)
    if user["role"] != "admin":
        raise HTTPException(403, "Réservé à l'animateur.")
    return user
